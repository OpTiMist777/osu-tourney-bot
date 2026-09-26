"""Bancho IRC match flow; IRC chat is restricted to BanchoBot and the MP room."""
from __future__ import annotations
import asyncio
from copy import deepcopy
import hashlib
import random
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
import discord
from discord import app_commands
from discord.ext import commands
from bancho_irc import BanchoIRC
from database import (
    create_match, get_match, get_pool_by_name, update_match,
    get_active_match_by_bancho_channel, get_match_by_bancho_channel,
    get_active_match_for_osu_users,
    get_live_matches,
    create_osu_login_challenge, delete_osu_login_challenge,
    complete_osu_login, get_osu_account_by_discord, get_osu_login_challenge,
    update_osu_account_username,
)
from osu_api import osu_manager
from rulesets import get_ruleset

logger = logging.getLogger("osu_tourney.matches")
LOGIN_CODE_PATTERN = re.compile(r"^OSU-[0-9A-F]{32}$")
SLOT_INPUT_PATTERN = re.compile(r"^(?:[A-Z]{1,3}\d+|TB)$")


class MultiplayerMod(str, Enum):
    """Tokens used to compose the enforced multiplayer mod combinations."""
    NO_FAIL = "nf"
    EASY = "ez"
    HIDDEN = "hd"
    HARD_ROCK = "hr"
    DOUBLE_TIME = "dt"
    FREE_MOD = "freemod"
    MIRROR = "mr"
    FADE_IN = "fi"
    FLASHLIGHT = "fl"


def _nick(value: str) -> str:
    return value.strip().strip('[]').replace('_', ' ').casefold()


def _invite_target(match: dict, player: str) -> str:
    """Use Bancho's unambiguous #<user_id> invite target when available."""
    for index, name in enumerate(match.get('players', [])):
        if _nick(name) != _nick(player):
            continue
        user_ids = match.get('player_osu_ids', [])
        if index < len(user_ids):
            try:
                return f"#{int(user_ids[index])}"
            except (TypeError, ValueError):
                break
    # Keep old matches usable if they were created before IDs were stored.
    return player.replace(" ", "_")


def _new_osu_login_code() -> str:
    return f"OSU-{secrets.token_hex(16).upper()}"


def _hash_osu_login_code(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()

def _actions(best_of: int, bans: int, first_ban: str, first_pick: str) -> list[dict]:
    result = [{"kind": "ban", "player": first_ban}, {"kind": "ban", "player": first_pick}]
    picks = [first_pick, first_ban]
    if bans == 2:
        result.extend({"kind": "pick", "player": player} for player in picks)
        result.extend(({"kind": "ban", "player": first_ban}, {"kind": "ban", "player": first_pick}))
    # TB is never picked or banned.  It is the final, deciding map: map 5 of
    # BO5, map 7 of BO7, and map 9 of BO9.  Pick only the other N-1 maps.
    regulation_picks = best_of - 1
    result.extend({"kind": "pick", "player": picks[index % 2]} for index in range(regulation_picks - sum(item["kind"] == "pick" for item in result)))
    return result


def _ordered_slots(maps: list[dict], mode: str) -> list[str]:
    """Keep the pool's ruleset category order (NM → HD → HR …), never lexicographic order."""
    ruleset = get_ruleset(mode)
    order = {category: index for index, category in enumerate(ruleset.category_order)}

    def key(item: dict) -> tuple[int, int, str]:
        slot = item["slot"].upper()
        category = ruleset.category_from_slot(slot)
        suffix = "".join(filter(str.isdigit, slot))
        return order.get(category, len(order)), int(suffix or 0), slot

    return [item["slot"].upper() for item in sorted(maps, key=key) if item["slot"].upper() != "TB"]


def _multiplayer_mods(slot: str, mode: str) -> str:
    """Return the ladder's enforced `!mp mods` combination for a picked slot."""
    ruleset = get_ruleset(mode)
    bancho_mods = {
        "nf": MultiplayerMod.NO_FAIL.value,
        "nofail": MultiplayerMod.NO_FAIL.value,
        "hd": MultiplayerMod.HIDDEN.value,
        "hidden": MultiplayerMod.HIDDEN.value,
        "hr": MultiplayerMod.HARD_ROCK.value,
        "hardrock": MultiplayerMod.HARD_ROCK.value,
        "dt": MultiplayerMod.DOUBLE_TIME.value,
        "doubletime": MultiplayerMod.DOUBLE_TIME.value,
        "fm": MultiplayerMod.FREE_MOD.value,
        "freemod": MultiplayerMod.FREE_MOD.value,
    }
    configured_mods = tuple(
        bancho_mods[modifier.replace(" ", "").casefold()]
        for modifier in ruleset.mods_for_slot(slot)
    )
    return " ".join(dict.fromkeys((MultiplayerMod.NO_FAIL.value, *configured_mods)))

def _mod_tokens(value: str) -> set[str]:
    normalized = value.casefold()
    for source, target in {
        'no fail': 'nofail',
        'hard rock': 'hardrock',
        'double time': 'doubletime',
        'free mod': 'freemod',
        'fade in': 'fadein',
        'fade-in': 'fadein',
    }.items():
        normalized = normalized.replace(source, target)
    aliases = {
        'nofail': 'nf', 'easy': 'ez', 'hidden': 'hd',
        'hardrock': 'hr', 'doubletime': 'dt', 'freemod': 'freemod',
        'mirror': 'mr', 'fadein': 'fi', 'flashlight': 'fl',
    }
    return {aliases.get(part.strip(), part.strip())
            for part in re.split(r'[,|+ ]+', normalized) if part.strip()}


def _freemod_allowed_tokens(mode: str, slot: str | None = None) -> set[str] | None:
    """Return allowed personal FreeMod tokens for a mode and optional slot."""
    ruleset = get_ruleset(mode)
    category = ruleset.category_from_slot(slot) if slot else None
    allowed_mods = ruleset.freemod_slot_allowed_mods.get(category, ruleset.freemod_allowed_mods)
    return _mod_tokens(','.join(allowed_mods)) if allowed_mods is not None else None


def _freemod_required_tokens(mode: str, slot: str | None = None) -> set[str]:
    """Return personal FreeMod tokens required for every player in a slot."""
    ruleset = get_ruleset(mode)
    category = ruleset.category_from_slot(slot) if slot else None
    required_mods = ruleset.freemod_slot_required_mods.get(category, ())
    return _mod_tokens(','.join(required_mods))


def _freemod_instruction(mode: str, slot: str | None = None) -> str | None:
    """Describe restricted FreeMod choices before players confirm readiness."""
    ruleset = get_ruleset(mode)
    category = ruleset.category_from_slot(slot) if slot else None
    allowed_mods = ruleset.freemod_slot_allowed_mods.get(category, ruleset.freemod_allowed_mods)
    if allowed_mods is None:
        return None
    required_tokens = _freemod_required_tokens(mode, slot)
    required_mods = [mod for mod in allowed_mods if _mod_tokens(mod) <= required_tokens]
    optional_mods = [
        mod for mod in allowed_mods
        if _mod_tokens(mod) != {'nf'} and not _mod_tokens(mod) <= required_tokens
    ]
    mode_name = {"std": "STD", "ctb": "CTB"}.get(mode, ruleset.mode.title())
    message = f'{mode_name} FreeMod: enable NoFail before Ready.'
    if required_mods:
        message += f' Required: {", ".join(required_mods)}.'
    if optional_mods:
        message += f' Optional mods: {", ".join(optional_mods)}.'
    return message


def _freemod_score_multiplier(mode: str, slot: str | None, player_mods: set[str]) -> float:
    """Return a configured multiplier only for a FreeMod pool slot."""
    multiplier = 1.0
    if not slot:
        return multiplier
    for modifier, value in get_ruleset(mode).score_multipliers_for_slot(slot).items():
        if _mod_tokens(modifier) <= player_mods:
            multiplier *= value
    return multiplier


def _match_player_mods(match: dict, settings: dict) -> dict[str, set[str]]:
    """Map settings' stable osu! IDs back to the persisted match player names."""
    by_user_id = {
        int(user_id): player
        for player, user_id in zip(match.get('players', []), match.get('player_osu_ids', []))
    }
    return {
        by_user_id[int(player['user_id'])]: _mod_tokens(','.join(player.get('mods', [])))
        for player in settings.get('players', [])
        if int(player.get('user_id', 0)) in by_user_id
    }

class OsuCommands(commands.Cog, name="osu! multiplayer"):
    ACTION_TIMER_SECONDS = 90
    READY_MESSAGES = {'all players are ready', 'all players ready'}
    MATCH_LOG_CHANNEL_ID = 1550058586846011473
    SCORE_WATCH_CHANNEL_ID = 1451591717101899929

    def __init__(self, bot: commands.Bot) -> None:
        self.bot, self.irc = bot, BanchoIRC()
        self.irc.message_handler = self._on_bancho_message
        self.irc.private_message_handler = self._on_bancho_private_message
        self.irc.join_handler = self._on_bancho_join
        self.irc.reconnect_handler = self._on_irc_reconnected
        if self.irc.configured:
            logger.info(
                "Bancho IRC client initialized; automatic startup connection is disabled, "
                "connection starts via /osu-connect or /match_create"
            )
        else:
            logger.warning(
                "Bancho IRC client initialized but will not start: "
                "BANCHO_USERNAME or BANCHO_IRC_PASSWORD is missing"
            )
        self._close_tasks: dict[int, asyncio.Task] = {}
        self._invite_tasks: dict[int, asyncio.Task] = {}
        self._action_timeout_tasks: dict[int, asyncio.Task] = {}
        self._ready_timeout_tasks: dict[int, asyncio.Task] = {}
        self._settings_tasks: dict[int, asyncio.Task] = {}
        self._action_locks: dict[int, asyncio.Lock] = {}
        self._match_create_lock = asyncio.Lock()

    def cog_unload(self) -> None:
        # discord.py calls cog_unload synchronously; do not leave an unawaited
        # coroutine behind when the extension is reloaded or the bot exits.
        for task in self._close_tasks.values():
            task.cancel()
        for task in self._invite_tasks.values():
            task.cancel()
        for task in self._action_timeout_tasks.values():
            task.cancel()
        for task in self._ready_timeout_tasks.values():
            task.cancel()
        for task in self._settings_tasks.values():
            task.cancel()
        self.bot.loop.create_task(self.irc.close())

    def _action_lock(self, match_id: int) -> asyncio.Lock:
        """Return the per-match lock shared by slot messages and timeouts."""
        return self._action_locks.setdefault(match_id, asyncio.Lock())

    async def _get_discord_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        """Return a sendable Discord channel without assuming it is cached."""
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.DiscordException:
                return None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    async def _post_match_log(self, match_id: int, message: str, *, level: str = "INFO") -> None:
        """Mirror a concise, non-sensitive match event to the staff log channel."""
        try:
            channel = await self._get_discord_channel(self.MATCH_LOG_CHANNEL_ID)
            if channel is None:
                logger.warning("Match #%s: Discord log channel is unavailable", match_id)
                return
            timestamp = datetime.now().strftime("%H:%M:%S")
            await channel.send(
                f"```text\n[{timestamp}] [{level}] Match #{match_id}: {message[:1800]}\n```",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            logger.warning("Match #%s: failed to send an event to Discord logs", match_id)

    async def _refresh_match_log_embed(self, match: dict) -> None:
        """Keep one full live-match embed synchronized in the score-watch channel."""
        try:
            channel = await self._get_discord_channel(self.SCORE_WATCH_CHANNEL_ID)
            if channel is None:
                logger.warning("Match #%s: Discord score-watch channel is unavailable", match["match_id"])
                return

            message_id = match.get("match_log_message_id")
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(embed=self._embed(match))
                    return
                except discord.NotFound:
                    # The staff message may be deleted manually. Recreate it
                    # and replace only its stored reference.
                    pass

            message = await channel.send(
                embed=self._embed(match),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await update_match(match["match_id"], {"match_log_message_id": message.id})
        except discord.DiscordException:
            logger.warning("Match #%s: failed to update the score-watch embed", match["match_id"])

    async def _send_match_pool(self, match: dict) -> None:
        """Post the exact stored pool to the match's Discord channel once."""
        if match.get("pool_message_id"):
            return
        try:
            channel = await self._get_discord_channel(int(match["discord_channel_id"]))
            pool_cog = self.bot.get_cog("Pool Commands")
            if channel is None or pool_cog is None:
                raise RuntimeError("match channel or Pool Commands cog is unavailable")
            embed, pool = await pool_cog._pool_view_embed(int(match["pool_id"]))
            if embed is None or pool is None:
                raise RuntimeError("match pool not found")
            embed.title = f"🗺️ Match pool: {pool['name']}"
            embed.set_footer(text=f"Mode: {pool['mode'].upper()} · BO{match['best_of']}")
            message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await update_match(match["match_id"], {"pool_message_id": message.id})
            await self._post_match_log(match["match_id"], f"Pool posted to the match channel: {pool['name']}.")
        except Exception:
            logger.exception("Match #%s: failed to publish the pool in Discord", match["match_id"])
            await self._post_match_log(
                match["match_id"], "Could not post the pool to the match channel.", level="ERROR",
            )

    async def restore_live_matches(self) -> None:
        """Rejoin and resume persisted live rooms after a bot process restart."""
        matches = await get_live_matches()
        if not matches:
            logger.info("Bancho IRC: no active matches need recovery")
            return
        if not self.irc.configured:
            logger.error("Bancho IRC: cannot recover %s matches because IRC is not configured", len(matches))
            return
        logger.info("Bancho IRC: recovering active matches: %s", len(matches))
        for match in matches:
            try:
                await self.irc.restore_channel(match['bancho_channel'])
            except Exception:
                logger.exception("Match #%s: failed to recover the MP room", match['match_id'])

    @staticmethod
    def _embed(match: dict) -> discord.Embed:
        text = (
            f"Room: [#{match['bancho_match_id']}](https://osu.ppy.sh/community/matches/{match['bancho_match_id']})\n"
            f"Pool: **{match['pool_name']}** · `{match['mode'].upper()}` · BO{match['best_of']}"
        )
        status_colors = {
            'waiting_players': 0xFEE75C,
            'pickban': 0x5865F2,
            'waiting_ready': 0x5865F2,
            'checking_settings': 0x5865F2,
            'game_running': 0x57F287,
            'completed': 0x57F287,
            'cancelled': 0xED4245,
        }
        embed = discord.Embed(
            title=f"🎮 Match #{match['match_id']}",
            description=text,
            color=status_colors.get(match.get('status'), 0x5865F2),
        )
        players = match['players']
        series = match.get('series_score', {})
        embed.add_field(
            name="Series score",
            value=f"**{players[0]}** `{series.get(players[0], 0)}` — `{series.get(players[1], 0)}` **{players[1]}**",
            inline=False,
        )
        feed_lines = []
        ban_slots = [
            item.get('slot') or '—'
            for item in match.get('history', [])
            if item.get('kind') == 'ban'
        ]
        if ban_slots:
            embed.add_field(name="Bans", value=" · ".join(f"`{slot}`" for slot in ban_slots), inline=False)
        played_maps = match.get('played_maps', [])
        played_by_slot = {}
        for played in played_maps:
            played_by_slot.setdefault(played.get('slot'), []).append(played)

        def format_result(played: dict) -> str:
            scores = played.get('scores', {})
            first_score = scores.get(players[0], '?')
            second_score = scores.get(players[1], '?')
            label = "TB" if played.get('is_tiebreaker') else played['slot']
            first_value = f"{first_score:,}" if isinstance(first_score, int) else str(first_score)
            second_value = f"{second_score:,}" if isinstance(second_score, int) else str(second_score)
            winner = played.get('winner')
            if winner == players[0]:
                first_result, second_result = "🟩 W", "🟥 L"
            elif winner == players[1]:
                first_result, second_result = "🟥 L", "🟩 W"
            else:
                first_result = second_result = "⚪ DRAW"
            return (
                f"`{label}` — {first_result} **{players[0]}** `{first_value}` — "
                f"`{second_value}` **{players[1]}** {second_result}"
            )

        # Walk the persisted action history to preserve the exact match order.
        # Pick actions themselves stay hidden; their completed map is rendered
        # at that position in the feed.
        for item in match.get('history', []):
            kind = item.get('kind')
            slot = item.get('slot')
            if kind in {'pick', 'tiebreaker'} and played_by_slot.get(slot):
                feed_lines.append(format_result(played_by_slot[slot].pop(0)))

        # Keep the feed robust for old records that have results but no matching
        # pick entry in history.
        for remaining in played_by_slot.values():
            for played in remaining:
                feed_lines.append(format_result(played))
        if feed_lines:
            embed.add_field(name="Match feed", value="\n".join(feed_lines)[-1024:], inline=False)
        if match['status'] == 'waiting_players':
            joined = match.get('joined_players', [])
            embed.add_field(
                name="Waiting for players",
                value=f"Joined: `{', '.join(joined) if joined else 'nobody yet'}`\n"
                      "The roll starts after both players join the lobby.",
                inline=False,
            )
        elif match['status'] in {'pickban', 'waiting_ready', 'checking_settings', 'game_running'}:
            embed.add_field(name="🔴 Live", value="\u200b", inline=False)
        elif match['status'] == 'completed':
            winner = max(series, key=series.get) if series else '—'
            embed.add_field(
                name="Winner",
                value=f"**{winner}**",
                inline=False,
            )
        elif match['status'] == 'cancelled':
            embed.add_field(
                name="Match cancelled",
                value="Both players did not join the lobby within the five-minute limit.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Match status",
                value=f"Current state: `{match.get('status', 'unknown')}`.",
                inline=False,
            )
        return embed

    @staticmethod
    def _created_embed(match: dict) -> discord.Embed:
        """Build the compact, continuously updated match card for its Discord channel."""
        players = match.get('players', ['—', '—'])
        status = match.get('status', 'waiting_players')
        joined = match.get('joined_players', [])
        if status == 'waiting_players':
            description = (
                "Lobby is ready. Players must join the room; the roll starts "
                "as soon as both participants are inside."
            )
            status_text = f"⏳ Lobby: {len(joined)}/2"
            color = 0xFEE75C
        elif status == 'pickban':
            description = "Both players are in the lobby. Pick/ban is handled in MP chat."
            status_text = "🎲 Pick/ban in progress"
            color = 0x5865F2
        elif status in {'waiting_ready', 'checking_settings'}:
            description = "The selected map is set. The bot is checking lobby readiness before starting."
            status_text = "⏳ Preparing map"
            color = 0x5865F2
        elif status == 'game_running':
            description = "The map has started. Results will appear after the game ends."
            status_text = "🟢 Game in progress"
            color = 0x57F287
        elif status == 'completed':
            series = match.get('series_score', {})
            winner = max(series, key=series.get) if series else '—'
            description = f"Match complete. Winner: **{winner}**."
            status_text = "✅ Complete"
            color = 0x57F287
        else:
            description = "Match cancelled: both players did not join the lobby in time."
            status_text = "❌ Cancelled"
            color = 0xED4245
        embed = discord.Embed(
            title=f"🎮 Match #{match['match_id']}",
            description=description,
            color=color,
        )
        embed.add_field(
            name="Players",
            value=f"**{players[0]}**  vs  **{players[1]}**",
            inline=False,
        )
        embed.add_field(
            name="Pool",
            value=f"**{match['pool_name']}** · `{match['mode'].upper()}` · BO{match['best_of']}",
            inline=False,
        )
        embed.add_field(
            name="MP room",
            value=f"[Open room #{match['bancho_match_id']}](https://osu.ppy.sh/community/matches/{match['bancho_match_id']})",
            inline=True,
        )
        embed.add_field(
            name="Status",
            value=status_text,
            inline=True,
        )
        embed.set_footer(text="Detailed live status: score watch channel")
        return embed

    async def pool_name_autocomplete(self, interaction: discord.Interaction, current: str):
        from database import list_pools
        pools = await list_pools(status='ranked')
        return [app_commands.Choice(name=p['name'][:100], value=p['name'][:100]) for p in pools if current.casefold() in p['name'].casefold()][:25]

    @app_commands.command(name='osu-connect', description='Link your osu! account using a one-time PM code')
    async def osu_connect(self, interaction: discord.Interaction):
        """Start a one-time Discord <-> osu! account ownership challenge."""
        if not self.irc.configured:
            await interaction.response.send_message(
                '❌ Bancho IRC is not configured. Add BANCHO_USERNAME and BANCHO_IRC_PASSWORD to your private .env file.',
                ephemeral=True,
            )
            return

        existing = await get_osu_account_by_discord(interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"✅ osu! account **{existing['osu_username']}** is already linked.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        code = _new_osu_login_code()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        if not await create_osu_login_challenge(
            interaction.user.id, _hash_osu_login_code(code), expires_at
        ):
            await interaction.followup.send('❌ Could not create a linking code. Please try again later.', ephemeral=True)
            return

        self.irc.set_keep_connected(True)
        try:
            await self.irc.connect()
        except Exception:
            await delete_osu_login_challenge(interaction.user.id)
            logger.exception("Could not connect Bancho IRC for osu-connect")
            await interaction.followup.send(
                '❌ Could not connect to Bancho IRC. Check your network and try again.',
                ephemeral=True,
            )
            return

        logger.info("Bancho IRC connection is active; waiting for the osu-connect verification code")
        bot_name = self.irc.username.replace('_', ' ')
        await interaction.followup.send(
            f"Open osu! and send only this code in a PM to **{bot_name}**:\n"
            f"`{code}`\n\nThe code expires in 10 minutes and can only be used once.",
            ephemeral=True,
        )

    async def _on_bancho_private_message(self, sender: str, text: str) -> None:
        """Consume an exact one-time code received in osu! PM."""
        code = text.strip().upper()
        if not LOGIN_CODE_PATTERN.fullmatch(code):
            return
        code_hash = _hash_osu_login_code(code)
        challenge = await get_osu_login_challenge(code_hash)
        if not challenge:
            return

        osu_username = sender.strip().strip('[]')
        logger.info("osu-connect: received a valid code from osu! user %s", osu_username)
        try:
            profile = await osu_manager.get_user(osu_username)
        except Exception:
            logger.warning("osu-connect: could not verify osu! sender %s", osu_username)
            try:
                await self.irc.send_private_message(
                    sender,
                    'Could not verify this osu! account. Check that the profile is available and send the code again.',
                )
            except Exception:
                logger.exception("osu-connect: failed to send a reply to user %s", osu_username)
            return

        status, account = await complete_osu_login(
            code_hash, profile['id'], profile['username']
        )
        logger.info("osu-connect: linking result for %s: %s", profile['username'], status)
        messages = {
            'linked': f"✅ osu! account {profile['username']} has been linked to Discord.",
            'already_linked': f"✅ osu! account {profile['username']} is already linked to this Discord account.",
            'osu_already_linked': '❌ This osu! account is already linked to another Discord account.',
            'discord_already_linked': '❌ This Discord account already has another osu! account linked.',
            'storage_error': '❌ Could not save the link. Please try again in a few seconds.',
        }
        try:
            await self.irc.send_private_message(sender, messages.get(status, '❌ Could not complete the account link.'))
        except Exception:
            logger.exception("osu-connect: failed to send the result by PM to %s", osu_username)

        if status in {'linked', 'already_linked'} and account:
            try:
                discord_user = self.bot.get_user(int(account['discord_user_id']))
                if discord_user is None:
                    discord_user = await self.bot.fetch_user(int(account['discord_user_id']))
                await discord_user.send(f"✅ osu! account **{profile['username']}** has been linked successfully.")
            except discord.DiscordException:
                logger.warning("osu-connect: failed to send a Discord DM to user %s", account['discord_user_id'])

    async def _refresh_linked_account_username(self, account: dict) -> dict:
        """Refresh a linked account's cached username before match registration."""
        osu_user_id = int(account['osu_user_id'])
        profile = await osu_manager.get_user_by_id(osu_user_id)
        if int(profile['id']) != osu_user_id:
            raise RuntimeError(f"osu! API returned an unexpected ID for account {osu_user_id}")
        username = str(profile.get('username', '')).strip()
        if not username:
            raise RuntimeError(f"osu! API returned no username for account {osu_user_id}")

        old_username = str(account.get('osu_username', '')).strip()
        if old_username != username:
            if await update_osu_account_username(osu_user_id, username):
                logger.info(
                    "match_create: refreshed osu! ID %s before the match: %s -> %s",
                    osu_user_id, old_username or '<empty>', username,
                )
            else:
                logger.warning(
                    "match_create: current name for osu! ID %s is %s, but MongoDB was not updated",
                    osu_user_id, username,
                )
        return {**account, 'osu_username': username}

    @app_commands.command(name='match_create', description='Create a Bancho lobby and start in-game pick/ban')
    @app_commands.describe(player_one='Server member with a linked osu! account', player_two='Second server member with a linked osu! account', pool_name='Ranked pool', format='Match format')
    @app_commands.choices(format=[app_commands.Choice(name='BO5 — 1 ban', value='5:1'), app_commands.Choice(name='BO7 — 1 ban', value='7:1'), app_commands.Choice(name='BO7 — 2 bans', value='7:2'), app_commands.Choice(name='BO9 — 2 bans', value='9:2')])
    @app_commands.autocomplete(pool_name=pool_name_autocomplete)
    async def match_create(
        self,
        interaction: discord.Interaction,
        player_one: discord.Member,
        player_two: discord.Member,
        pool_name: str,
        format: app_commands.Choice[str],
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                '❌ Matches can only be created inside a Discord server.', ephemeral=True,
            )
            return
        creator_account = await get_osu_account_by_discord(interaction.user.id)
        if not creator_account or not str(creator_account.get('osu_username', '')).strip():
            logger.info("match_create rejected: Discord ID %s has no linked osu! account", interaction.user.id)
            await interaction.response.send_message(
                '❌ Link your osu! account first with `/osu-connect`.', ephemeral=True,
            )
            return
        if not self.irc.configured:
            await interaction.response.send_message('❌ Add BANCHO_USERNAME and BANCHO_IRC_PASSWORD to your private .env, then restart the bot.', ephemeral=True); return
        if player_one.id == player_two.id:
            await interaction.response.send_message('❌ Choose two different server members.', ephemeral=True); return
        player_one_account = await get_osu_account_by_discord(player_one.id)
        player_two_account = await get_osu_account_by_discord(player_two.id)
        missing_accounts = [
            member.mention
            for member, account in (
                (player_one, player_one_account),
                (player_two, player_two_account),
            )
            if not account or not str(account.get('osu_username', '')).strip()
        ]
        if missing_accounts:
            logger.info(
                "match_create rejected: participants have no linked osu! accounts: %s",
                ', '.join(str(member.id) for member, account in (
                    (player_one, player_one_account), (player_two, player_two_account)
                ) if not account or not str(account.get('osu_username', '')).strip()),
            )
            await interaction.response.send_message(
                f"❌ These members must link their osu! accounts first with `/osu-connect`: {', '.join(missing_accounts)}.",
                ephemeral=True,
            )
            return
        active_player_match = await get_active_match_for_osu_users([
            int(player_one_account['osu_user_id']),
            int(player_two_account['osu_user_id']),
        ])
        if active_player_match:
            await interaction.response.send_message(
                f"❌ One of the selected players is already in an active match "
                f"#{active_player_match['match_id']}.",
                ephemeral=True,
            )
            return
        pools = [p for p in await get_pool_by_name(pool_name) if p['status'] == 'ranked']
        if len(pools) != 1:
            await interaction.response.send_message('❌ No Ranked pool with that name was found.', ephemeral=True); return
        pool = pools[0]; best_of, bans = map(int, format.value.split(':'))
        slots = _ordered_slots(pool['maps'], pool['mode'])
        tiebreaker = next((item['slot'].upper() for item in pool['maps'] if item['slot'].upper() == 'TB'), None)
        required_slots = (best_of - 1) + 2 * bans
        if not tiebreaker:
            await interaction.response.send_message('❌ The Ranked pool is missing the required `TB` map.', ephemeral=True); return
        if len(slots) < required_slots:
            await interaction.response.send_message('❌ The pool does not have enough non-TB slots for this format.', ephemeral=True); return
        await interaction.response.defer(thinking=True)
        try:
            player_one_account = await self._refresh_linked_account_username(player_one_account)
            player_two_account = await self._refresh_linked_account_username(player_two_account)
        except Exception:
            logger.exception("match_create: failed to refresh player osu! names by ID")
            await interaction.followup.send(
                '❌ Could not refresh the players’ current osu! names. Please try again later.',
                ephemeral=True,
            )
            return
        player_one_name = str(player_one_account['osu_username']).strip()
        player_two_name = str(player_two_account['osu_username']).strip()
        if _nick(player_one_name) == _nick(player_two_name):
            logger.warning(
                "match_create rejected after name refresh: Discord IDs %s and %s are linked to the same osu! account",
                player_one.id, player_two.id,
            )
            await interaction.followup.send(
                '❌ The selected members cannot be linked to the same osu! account.',
                ephemeral=True,
            )
            return
        async with self._match_create_lock:
            # Repeat the active-match check after the username refresh and
            # immediately before room creation. This closes the race where two
            # concurrent slash commands both pass the initial check.
            active_player_match = await get_active_match_for_osu_users([
                int(player_one_account['osu_user_id']),
                int(player_two_account['osu_user_id']),
            ])
            if active_player_match:
                await interaction.followup.send(
                    f"❌ One of the selected players is already in an active match "
                    f"#{active_player_match['match_id']}.",
                    ephemeral=True,
                )
                return
            try:
                logger.info(
                    "Creating match: %s (Discord ID %s) vs %s (Discord ID %s), pool '%s', %s",
                    player_one_name, player_one.id, player_two_name, player_two.id, pool['name'], format.name,
                )
                channel = await self.irc.make_match(f'Ladder test — {player_one_name} vs {player_two_name}')
            except Exception as error:
                logger.exception("Failed to create Bancho lobby")
                await interaction.followup.send(f'❌ Failed to create the Bancho lobby: {error}', ephemeral=True); return
            winner, loser = (player_one_name, player_two_name) if random.choice((True, False)) else (player_two_name, player_one_name)
            map_choices = {
                item['slot'].upper(): {
                    'beatmap_id': item['beatmap_id'],
                    'mods': _multiplayer_mods(item['slot'], pool['mode']),
                }
                for item in pool['maps']
            }
            try:
                match_id = await create_match({'status':'waiting_players', 'discord_channel_id':interaction.channel_id, 'discord_message_id':None, 'pool_message_id':None, 'match_log_message_id':None, 'bancho_channel':channel, 'bancho_match_id':channel.removeprefix('#mp_'), 'pool_id':pool['pool_id'], 'pool_name':pool['name'], 'mode':pool['mode'], 'players':[player_one_name, player_two_name], 'player_osu_ids':[int(player_one_account['osu_user_id']), int(player_two_account['osu_user_id'])], 'joined_players':[], 'join_deadline':datetime.now(timezone.utc) + timedelta(minutes=5), 'roll_winner':winner, 'roll_loser':loser, 'best_of':best_of, 'bans_per_player':bans, 'actions':_actions(best_of,bans,loser,winner), 'action_index':0, 'available_slots':slots, 'tiebreaker_slot':tiebreaker, 'map_choices':map_choices, 'history':[]})
            except Exception as error:
                logger.exception("Match created in Bancho but not saved to MongoDB: %s", channel)
                try:
                    await self.irc.send_channel(channel, '!mp close')
                    self.irc.forget_channel(channel)
                except Exception:
                    logger.exception("Failed to close orphaned Bancho lobby %s", channel)
                await interaction.followup.send(
                    '❌ The lobby was created but the match could not be saved. The lobby was closed; please try again.',
                    ephemeral=True,
                )
                return
        logger.info("Match #%s saved: %s, roll winner=%s", match_id, channel, winner)
        await self._post_match_log(
            match_id,
            f"Lobby {channel} created: {player_one_name} vs {player_two_name}; "
            f"pool {pool['name']}; BO{best_of}.",
        )
        await self.irc.send_channel(channel, 'Waiting for both players to join before the roll.')
        match = await get_match(match_id)
        message = await interaction.followup.send(
            embed=self._created_embed(match),
            allowed_mentions=discord.AllowedMentions.none(),
            wait=True,
        )
        await update_match(match_id, {'discord_message_id': message.id})
        await self._refresh_match_log_embed(match)
        self._invite_tasks[match_id] = asyncio.create_task(
            self._send_invites_for_five_minutes(match_id, channel, [player_one_name, player_two_name])
        )

    async def _send_invites_for_five_minutes(self, match_id: int, channel: str, players: list[str]) -> None:
        """Send one invite round per minute during the full five-minute join window."""
        try:
            attempt = 0
            while True:
                match = await get_match_by_bancho_channel(channel)
                if not match or match['status'] != 'waiting_players':
                    return
                deadline = match.get('join_deadline')
                if not isinstance(deadline, datetime):
                    # Matches created before the deadline was persisted get a
                    # fresh full window when they are recovered after restart.
                    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
                    await update_match(match_id, {'join_deadline': deadline})
                elif deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                seconds_left = (deadline - datetime.now(timezone.utc)).total_seconds()
                if seconds_left <= 0:
                    break
                attempt += 1
                joined = {_nick(name) for name in match.get('joined_players', [])}
                for player in players:
                    if _nick(player) not in joined:
                        await self.irc.send_channel(channel, f'!mp invite {_invite_target(match, player)}')
                logger.info("Match #%s: invite round %s, %.0f s remaining", match_id, attempt, seconds_left)
                await asyncio.sleep(min(60, seconds_left))
            logger.info("Match #%s: five-minute join window ended", match_id)
            match = await get_match_by_bancho_channel(channel)
            if match and match['status'] == 'waiting_players':
                joined = match.get('joined_players', [])
                await update_match(match_id, {'status': 'cancelled'})
                logger.info("Match #%s: cancelled; not all players joined (%s/2)", match_id, len(joined))
                await self._post_match_log(
                    match_id, f"Match cancelled: only {len(joined)}/2 players joined within 5 minutes.", level="WARNING",
                )
                await self.irc.send_channel(
                    channel,
                    f"Match cancelled: not all players joined within 5 minutes ({len(joined)}/2).",
                )
                await self.irc.send_channel(channel, '!mp close')
                self.irc.forget_channel(channel)
                await self._refresh_discord(match_id)
        except asyncio.CancelledError:
            logger.info("Match #%s: repeated invites stopped; both players joined", match_id)
        except Exception:
            logger.exception("Match #%s: failed to resend invites", match_id)
        finally:
            self._invite_tasks.pop(match_id, None)

    async def _on_bancho_join(self, sender: str, channel: str) -> None:
        """Handle the IRC JOIN event emitted when a player enters the MP chat."""
        await self._mark_player_joined(sender, channel)

    async def _canonicalize_match_player(
        self, match: dict, observed_name: str,
    ) -> tuple[dict, str | None]:
        """Resolve a renamed player by stable osu! ID and update match state."""
        known = {
            _nick(name): name
            for name in match.get('players', [])
        }
        canonical = known.get(_nick(observed_name))
        if canonical:
            return match, canonical

        user_ids = match.get('player_osu_ids', [])
        if not user_ids:
            return match, None
        try:
            profile = await osu_manager.get_user(observed_name)
            osu_user_id = int(profile['id'])
            player_index = next(
                index for index, user_id in enumerate(user_ids)
                if int(user_id) == osu_user_id
            )
            canonical = str(profile['username']).strip()
        except Exception:
            logger.debug(
                "Match #%s: username %s could not be matched to participants by osu! ID",
                match.get('match_id', '?'), observed_name,
            )
            return match, None
        if not canonical:
            return match, None

        old_name = match['players'][player_index]
        if old_name == canonical:
            return match, canonical

        def replace_name(value):
            return canonical if isinstance(value, str) and _nick(value) == _nick(old_name) else value

        updates: dict = {
            'players': [replace_name(name) for name in match.get('players', [])],
            'joined_players': [replace_name(name) for name in match.get('joined_players', [])],
            'roll_winner': replace_name(match.get('roll_winner')),
            'roll_loser': replace_name(match.get('roll_loser')),
        }
        updates['actions'] = deepcopy(match.get('actions', []))
        for action in updates['actions']:
            if 'player' in action:
                action['player'] = replace_name(action['player'])

        updates['history'] = deepcopy(match.get('history', []))
        for item in updates['history']:
            if 'player' in item:
                item['player'] = replace_name(item['player'])

        updates['series_score'] = {
            replace_name(name): score
            for name, score in match.get('series_score', {}).items()
        }
        for name in updates['players']:
            updates['series_score'].setdefault(name, 0)
        updates['current_map_scores'] = {
            replace_name(name): score
            for name, score in match.get('current_map_scores', {}).items()
        }
        updates['current_map_player_mods'] = {
            replace_name(name): mods
            for name, mods in match.get('current_map_player_mods', {}).items()
        }
        updates['played_maps'] = deepcopy(match.get('played_maps', []))
        for played in updates['played_maps']:
            if 'winner' in played:
                played['winner'] = replace_name(played['winner'])
            if isinstance(played.get('scores'), dict):
                played['scores'] = {
                    replace_name(name): score
                    for name, score in played['scores'].items()
                }
            if isinstance(played.get('raw_scores'), dict):
                played['raw_scores'] = {
                    replace_name(name): score
                    for name, score in played['raw_scores'].items()
                }
            if isinstance(played.get('score_multipliers'), dict):
                played['score_multipliers'] = {
                    replace_name(name): multiplier
                    for name, multiplier in played['score_multipliers'].items()
                }

        if not await update_osu_account_username(osu_user_id, canonical):
            logger.warning(
                "Match #%s: username for osu! ID %s was updated only in the match; MongoDB was not updated",
                match.get('match_id', '?'), osu_user_id,
            )
        await update_match(match['match_id'], updates)
        match = {**match, **updates}
        logger.info(
            "Match #%s: detected osu! ID %s rename: %s -> %s",
            match['match_id'], osu_user_id, old_name, canonical,
        )
        return match, canonical

    async def _mark_player_joined(self, player: str, channel: str) -> None:
        match = await get_match_by_bancho_channel(channel)
        if not match or match['status'] != 'waiting_players':
            return
        expected = {_nick(name): name for name in match['players']}
        canonical = expected.get(_nick(player))
        if not canonical:
            match, canonical = await self._canonicalize_match_player(match, player)
        if not canonical or canonical in match.get('joined_players', []):
            return
        joined = [*match.get('joined_players', []), canonical]
        logger.info("Match #%s: %s joined the lobby (%s/2)", match['match_id'], canonical, len(joined))
        await self._post_match_log(
            match['match_id'], f"{canonical} joined the lobby ({len(joined)}/2).",
        )
        if len(joined) < len(match['players']):
            await update_match(match['match_id'], {'joined_players': joined})
            await self._refresh_discord(match['match_id'])
            return
        invite_task = self._invite_tasks.pop(match['match_id'], None)
        if invite_task and not invite_task.done():
            invite_task.cancel()
        await update_match(match['match_id'], {'joined_players': joined, 'status': 'pickban'})
        updated = await get_match(match['match_id'])
        await self._send_match_pool(updated)
        await self._post_match_log(
            updated['match_id'],
            f"Both players joined. Roll: {updated['roll_winner']} picks first; "
            f"{updated['roll_loser']} bans first.",
        )
        await self.irc.send_channel(channel, f'Both players joined. Roll result: {updated["roll_winner"]} picks first; {updated["roll_loser"]} bans first.')
        await self.irc.send_channel(channel, f'{updated["roll_loser"]}: ban a slot by writing its name in this lobby chat.')
        await self.irc.send_channel(
            channel,
            f'Available slots: {", ".join(updated.get("available_slots", [])) or "none"}.',
        )
        await self.irc.send_channel(channel, f'!mp timer {self.ACTION_TIMER_SECONDS}')
        self._schedule_action_timeout(updated['match_id'])
        await self._refresh_discord(updated['match_id'])

    async def _on_irc_reconnected(self, channel: str) -> None:
        """Restore match state and timers after a transient IRC disconnect."""
        match = await get_match_by_bancho_channel(channel)
        if not match:
            self.irc.forget_channel(channel)
            return
        match_id = match['match_id']
        logger.info("Match #%s: IRC recovered, status=%s", match_id, match['status'])
        try:
            if match['status'] == 'waiting_players':
                # IRC JOIN does not necessarily replay users already present in
                # an MP room, so recover them from a fresh settings snapshot.
                settings = await self.irc.get_match_settings(channel)
                expected = {_nick(player) for player in match['players']}
                for player in settings.get('players', []):
                    username = player.get('username', '')
                    # A player may have renamed their osu! account since the
                    # match was created; let the ID-based resolver handle
                    # names that no longer match the cached value.
                    if _nick(username) in expected or match.get('player_osu_ids'):
                        await self._mark_player_joined(username, channel)
                refreshed = await get_match_by_bancho_channel(channel)
                if refreshed and refreshed['status'] == 'waiting_players':
                    invite_task = self._invite_tasks.get(match_id)
                    if not invite_task or invite_task.done():
                        self._invite_tasks[match_id] = asyncio.create_task(
                            self._send_invites_for_five_minutes(
                                match_id, channel, refreshed['players'],
                            )
                        )
                    await self._refresh_discord(match_id)
                return

            if match['status'] == 'pickban':
                action = match['actions'][match['action_index']]
                await self.irc.send_channel(
                    channel,
                    f'Connection restored. {action["player"]}: {action["kind"]} a slot.'
                )
                await self.irc.send_channel(
                    channel,
                    f'Available slots: {", ".join(match.get("available_slots", [])) or "none"}.',
                )
                await self.irc.send_channel(channel, f'!mp timer {self.ACTION_TIMER_SECONDS}')
                self._schedule_action_timeout(match_id)
                await self._refresh_discord(match_id)
                return

            if match['status'] in ('waiting_ready', 'checking_settings'):
                old = self._settings_tasks.pop(match_id, None)
                if old and not old.done():
                    old.cancel()
                # A ready notification sent before the disconnect is no
                # longer enough: the room may have changed while IRC was
                # offline.  Wait for a fresh BanchoBot ready notification so
                # that the settings snapshot is taken after both players
                # confirm readiness again.
                await update_match(match_id, {'status': 'waiting_ready'})
                await self.irc.send_channel(
                    channel,
                    'Connection restored. Please confirm readiness again. '
                    'After "All players are ready", I will verify !mp settings.'
                )
                await self._refresh_discord(match_id)
                return

            if match['status'] == 'game_running':
                await self.irc.send_channel(channel, 'Connection restored. The current map continues.')
        except Exception:
            logger.exception("Match #%s: failed to recover state after reconnect", match_id)

    async def _refresh_discord(self, match_id: int) -> None:
        match = await get_match(match_id)
        if not match:
            return
        await self._refresh_match_card(match)
        await self._refresh_match_log_embed(match)

    async def _refresh_match_card(self, match: dict) -> None:
        """Keep the compact player-facing card in the match channel current."""
        message_id = match.get('discord_message_id')
        if not message_id:
            return
        try:
            channel = await self._get_discord_channel(int(match['discord_channel_id']))
            if channel is None:
                return
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=self._created_embed(match))
        except discord.NotFound:
            logger.warning("Match #%s: Discord card was deleted", match['match_id'])
            await update_match(match['match_id'], {'discord_message_id': None})
        except discord.DiscordException:
            logger.warning("Match #%s: failed to update the Discord card", match['match_id'])

    async def _set_map_and_wait_ready(self, match: dict, slot: str, *, is_tiebreaker: bool = False) -> None:
        """Apply one stored pool map, then wait for BanchoBot readiness."""
        choice = match['map_choices'][slot]
        mode_id = {'std': 0, 'taiko': 1, 'ctb': 2, 'mania': 3, 'mania4k': 3, 'mania7k': 3}[match['mode']]
        label = 'tiebreaker' if is_tiebreaker else 'picked map'
        logger.info(
            "Match #%s: setting %s %s (beatmap %s, mode %s, mods %s)",
            match['match_id'], label, slot, choice['beatmap_id'], mode_id, choice['mods'],
        )
        await self._post_match_log(
            match['match_id'],
            f"Set {label} {slot}: beatmap {choice['beatmap_id']}, mode {mode_id}, mods {choice['mods']}.",
        )
        await update_match(match['match_id'], {
            'status': 'waiting_ready', 'selected_slot': slot, 'selected_is_tiebreaker': is_tiebreaker,
            'current_map_scores': {}, 'current_map_player_mods': {},
        })
        await self.irc.send_channel(match['bancho_channel'], f"!mp map {choice['beatmap_id']} {mode_id}")
        await self.irc.send_channel(match['bancho_channel'], f"!mp mods {choice['mods']}")
        instruction = _freemod_instruction(match['mode'], slot) if 'freemod' in _mod_tokens(choice['mods']) else None
        if instruction:
            await self.irc.send_channel(match['bancho_channel'], instruction)
        await self.irc.send_channel(match['bancho_channel'], 'Map is set. Waiting for: All players are ready.')
        await self.irc.send_channel(match['bancho_channel'], f'!mp timer {self.ACTION_TIMER_SECONDS}')
        self._schedule_ready_timeout(match['match_id'], match['bancho_channel'])

    def _cancel_ready_timeout(self, match_id: int) -> None:
        task = self._ready_timeout_tasks.pop(match_id, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _schedule_ready_timeout(self, match_id: int, channel: str) -> None:
        self._cancel_ready_timeout(match_id)
        self._ready_timeout_tasks[match_id] = asyncio.create_task(
            self._ready_timeout(match_id, channel)
        )

    async def _ready_timeout(self, match_id: int, channel: str) -> None:
        """Force-start a configured map when the readiness window expires."""
        try:
            await asyncio.sleep(self.ACTION_TIMER_SECONDS)
            match = await get_match(match_id)
            if not match or match.get('status') != 'waiting_ready':
                return
            logger.info("Match #%s: readiness timer expired; checking the lobby before forced start", match_id)
            await self._post_match_log(
                match_id, 'Ready timer expired; verifying lobby before forced start.', level='WARNING',
            )
            await self.irc.send_channel(
                channel,
                'Ready timer expired. Verifying map and mods before forced start.',
            )
            await self._queue_settings_check(match_id, channel, force_start=True)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Match #%s: readiness timer error", match_id)
        finally:
            if self._ready_timeout_tasks.get(match_id) is asyncio.current_task():
                self._ready_timeout_tasks.pop(match_id, None)

    async def _queue_settings_check(self, match_id: int, channel: str, *, force_start: bool = False) -> bool:
        """Queue the mandatory settings check for one fresh ready event."""
        match = await get_match(match_id)
        if not match or match.get('status') != 'waiting_ready':
            return False
        self._cancel_ready_timeout(match_id)
        selected = match.get('selected_slot', 'the selected map')
        if force_start:
            logger.info("Match #%s: requesting !mp settings for forced start of %s", match_id, selected)
            await self._post_match_log(match_id, f"Ready timer expired for {selected}; requesting lobby settings.")
        else:
            logger.info("Match #%s: all players are ready for %s; requesting !mp settings", match_id, selected)
            await self._post_match_log(match_id, f"Both players are ready for {selected}; requesting lobby settings.")
        await update_match(match_id, {'status': 'checking_settings'})
        old = self._settings_tasks.pop(match_id, None)
        if old and not old.done():
            old.cancel()
        self._settings_tasks[match_id] = asyncio.create_task(
            self._check_settings_and_start(match_id, channel, force_start=force_start)
        )
        return True

    async def _check_settings_and_start(self, match_id: int, channel: str, *, force_start: bool = False) -> None:
        """Validate BanchoBot's settings response before every game start."""
        try:
            await asyncio.sleep(0.2)  # let the ready event finish propagating
            match = await get_match(match_id)
            if not match or match.get('status') != 'checking_settings':
                return
            settings = await self.irc.get_match_settings(channel)
            selected = match.get('selected_slot')
            choice = match.get('map_choices', {}).get(selected, {})
            logger.info(
                "Match #%s: !mp settings received: map=%s, active mods=%s, players=%s, ready=%s",
                match_id,
                settings.get('beatmap_id', '?'),
                settings.get('active_mods', 'None'),
                settings.get('player_count', 0),
                sum(player.get('status', '').casefold() == 'ready' for player in settings.get('players', [])),
            )
            errors: list[str] = []
            if settings.get('beatmap_id') != choice.get('beatmap_id'):
                errors.append(f"map {settings.get('beatmap_id', '?')} instead of {choice.get('beatmap_id', '?')}")
            required = _mod_tokens(choice.get('mods', ''))
            active = _mod_tokens(settings.get('active_mods', ''))
            if 'freemod' in required:
                # NoFail is a per-player mod in FreeMod rooms, not a global
                # room mod reported by `Active mods`.
                expected_global_mods = required - {'nf'}
                if active != expected_global_mods:
                    errors.append(
                        f"global mods {settings.get('active_mods', 'None')} instead of "
                        f"{choice.get('mods', 'nf')}"
                    )
                for player in settings.get('players', []):
                    player_mods = _mod_tokens(','.join(player.get('mods', [])))
                    if 'nf' not in player_mods:
                        errors.append(f"player {player.get('username', '?')} does not have NoFail enabled")
                    required_player_mods = _freemod_required_tokens(match['mode'], selected)
                    missing_mods = required_player_mods - player_mods
                    if missing_mods:
                        errors.append(
                            f"player {player.get('username', '?')} is missing required mods: "
                            f"{', '.join(sorted(missing_mods))}"
                        )
                    allowed_mods = _freemod_allowed_tokens(match['mode'], selected)
                    if allowed_mods is not None:
                        disallowed = player_mods - allowed_mods
                        if disallowed:
                            errors.append(
                                f"player {player.get('username', '?')} has disallowed mods: "
                                f"{', '.join(sorted(disallowed))}"
                            )
            elif active != required:
                errors.append(f"mods {settings.get('active_mods', 'None')} instead of {choice.get('mods', 'nf')}")
            if settings.get('player_count') != 2 or len(settings.get('players', [])) < 2:
                errors.append('both match players are not confirmed in the lobby')
            else:
                expected_ids = {int(user_id) for user_id in match.get('player_osu_ids', [])}
                actual_ids = {int(player['user_id']) for player in settings.get('players', [])}
                if expected_ids and actual_ids != expected_ids:
                    errors.append('the lobby contains different players')
            if (
                not force_start
                and len(settings.get('players', [])) >= 2
                and any(player.get('status', '').casefold() != 'ready' for player in settings.get('players', []))
            ):
                errors.append('not all players are Ready')
            if errors and not force_start:
                logger.warning("Match #%s: !mp settings validation failed: %s", match_id, '; '.join(errors))
                await self._post_match_log(
                    match_id, f"Lobby validation failed for {selected}: {'; '.join(errors)}.", level="WARNING",
                )
                await update_match(match_id, {'status': 'waiting_ready'})
                await self.irc.send_channel(channel, 'Lobby check failed: ' + '; '.join(errors) + '. Map/mods will be reapplied.')
                await self._set_map_and_wait_ready(await get_match(match_id), selected)
            else:
                if errors:
                    logger.warning(
                        "Match #%s: forced start ignores settings validation errors: %s",
                        match_id,
                        '; '.join(errors),
                    )
                    await self._post_match_log(
                        match_id,
                        f"Forced start: proceeding despite lobby validation errors for {selected}: "
                        f"{'; '.join(errors)}.",
                        level="WARNING",
                    )
                start_kind = 'forced start' if force_start else 'start'
                logger.info("Match #%s: !mp settings confirmed; %s map %s", match_id, start_kind, selected)
                await self._post_match_log(match_id, f"Lobby validation passed for {selected}; {start_kind}.")
                await update_match(match_id, {
                    'status': 'game_running',
                    'current_map_player_mods': {
                        player: sorted(mods)
                        for player, mods in _match_player_mods(match, settings).items()
                    },
                })
                await self.irc.send_channel(channel, '!mp aborttimer')
                await self.irc.send_channel(channel, '!mp start 5')
            await self._refresh_discord(match_id)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Match #%s: !mp settings validation error", match_id)
            if force_start:
                # The force-start deadline is terminal: a failed settings
                # request must not send the match back into another ready timer.
                logger.warning("Match #%s: forced start proceeding after settings check error", match_id)
                await self._post_match_log(
                    match_id,
                    "Forced start: settings check failed, starting the map anyway.",
                    level="WARNING",
                )
                await update_match(match_id, {'status': 'game_running', 'current_map_player_mods': {}})
                await self.irc.send_channel(channel, '!mp aborttimer')
                await self.irc.send_channel(channel, '!mp start 5')
                await self._refresh_discord(match_id)
            else:
                await update_match(match_id, {'status': 'waiting_ready'})
        finally:
            if self._settings_tasks.get(match_id) is asyncio.current_task():
                self._settings_tasks.pop(match_id, None)

    def _schedule_action_timeout(self, match_id: int) -> None:
        old = self._action_timeout_tasks.pop(match_id, None)
        if old and not old.done():
            old.cancel()
        self._action_timeout_tasks[match_id] = asyncio.create_task(self._action_timeout(match_id))

    async def _action_timeout(self, match_id: int) -> None:
        try:
            await asyncio.sleep(self.ACTION_TIMER_SECONDS)
            # The same lock is held while handling a manual slot message. A
            # slot arriving on the timer boundary therefore cannot be applied
            # together with the automatic fallback for the same action.
            async with self._action_lock(match_id):
                match = await get_match(match_id)
                if not match or match['status'] != 'pickban':
                    return
                action = match['actions'][match['action_index']]
                if action['kind'] == 'ban':
                    logger.info(
                        "Match #%s: turn timeout for %s; ban skipped (%s)",
                        match_id, action['player'], action['kind'],
                    )
                    await self.irc.send_channel(match['bancho_channel'], f"{action['player']} did not ban in time. Ban skipped.")
                    await self._post_match_log(match_id, f"{action['player']} did not ban in time; ban skipped.", level="WARNING")
                    await self._advance_action(match, None, automatic=True)
                else:
                    if not match['available_slots']:
                        logger.error("Match #%s: pick timed out but no slots remain", match_id)
                        return
                    slot = random.SystemRandom().choice(match['available_slots'])
                    logger.info("Match #%s: turn timed out for %s; pseudo-random pick %s", match_id, action['player'], slot)
                    await self.irc.send_channel(match['bancho_channel'], f"{action['player']} did not pick in time. Pseudo-random pick: {slot}.")
                    await self._post_match_log(
                        match_id, f"{action['player']} did not pick in time; pseudo-random pick {slot}.", level="WARNING",
                    )
                    await self._advance_action(match, slot, automatic=True)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Match #%s: action timeout handling failed", match_id)
        finally:
            if self._action_timeout_tasks.get(match_id) is asyncio.current_task():
                self._action_timeout_tasks.pop(match_id, None)

    async def _advance_action(self, match: dict, slot: str | None, *, automatic: bool = False) -> None:
        action = match['actions'][match['action_index']]
        history = list(match['history'])
        if slot is not None:
            history.append({'kind': action['kind'], 'player': action['player'], 'slot': slot, 'automatic': automatic})
        else:
            history.append({'kind': 'ban_skip', 'player': action['player'], 'slot': None, 'automatic': True})
        next_index = match['action_index'] + 1
        status = 'waiting_ready' if action['kind'] == 'pick' and slot is not None else ('pickban' if next_index < len(match['actions']) else 'completed')
        await update_match(match['match_id'], {
            'history': history,
            'available_slots': [x for x in match['available_slots'] if x != slot] if slot else match['available_slots'],
            'action_index': next_index,
            'status': status,
            'selected_slot': slot if action['kind'] == 'pick' and slot else None,
        })
        logger.info(
            "Match #%s: %s %s%s processed; next status=%s (%s/%s)",
            match['match_id'],
            action['kind'],
            slot or 'skipped',
            ' automatically' if automatic else '',
            status,
            next_index,
            len(match['actions']),
        )
        if slot is not None:
            await self._post_match_log(
                match['match_id'],
                f"{'Automatic ' if automatic else ''}{action['kind'].title()}: {slot}.",
            )
        updated = await get_match(match['match_id'])
        if action['kind'] == 'pick' and slot:
            await self._set_map_and_wait_ready(updated, slot)
        elif status == 'pickban':
            turn = updated['actions'][next_index]
            await self.irc.send_channel(match['bancho_channel'], f"{action['player']} {'skipped the ban' if slot is None else f'used {slot}'}. {turn['player']}: {turn['kind']} a slot.")
            await self.irc.send_channel(
                match['bancho_channel'],
                f'Available slots: {", ".join(updated.get("available_slots", [])) or "none"}.',
            )
            await self.irc.send_channel(match['bancho_channel'], f'!mp timer {self.ACTION_TIMER_SECONDS}')
            self._schedule_action_timeout(match['match_id'])
        await self._refresh_discord(match['match_id'])

    async def _on_bancho_message(self, sender: str, channel: str, text: str) -> None:
        live_match = await get_match_by_bancho_channel(channel)
        # BanchoBot sends this only once everyone currently in the lobby is
        # ready.  We wait for it after *each* picked map, then start exactly
        # once.  Player chat cannot spoof this event.
        if (
            live_match
            and live_match['status'] == 'waiting_ready'
            and sender.casefold() == 'banchobot'
            and text.strip().casefold() in self.READY_MESSAGES
        ):
            logger.info("Match #%s: BanchoBot reported that all players are ready", live_match['match_id'])
            if await self._queue_settings_check(live_match['match_id'], channel):
                await self._refresh_discord(live_match['match_id'])
                return
        if live_match and live_match['status'] == 'game_running' and sender.casefold() == 'banchobot':
            score_line = re.match(r"^(.+?) finished playing \(Score: ([\d,]+), (?:PASSED|FAILED)\)\.?$", text, re.I)
            if score_line:
                expected = {_nick(player): player for player in live_match['players']}
                observed_player = score_line.group(1)
                player = expected.get(_nick(observed_player))
                if not player:
                    live_match, player = await self._canonicalize_match_player(live_match, observed_player)
                if player:
                    scores = dict(live_match.get('current_map_scores', {}))
                    scores[player] = int(score_line.group(2).replace(',', ''))
                    await update_match(live_match['match_id'], {'current_map_scores': scores})
                    logger.info("Match #%s: score %s = %s", live_match['match_id'], player, scores[player])
                return
            if text.strip().casefold() == 'the match has finished!':
                await self._finish_played_map(live_match)
                return
        # Some Bancho clients expose lobby arrivals only as BanchoBot text,
        # rather than an IRC JOIN event. Recognise that form as well.
        if sender.casefold() == 'banchobot':
            joined_notice = __import__('re').match(r"^(.+?) joined in slot \d+\.?$", text, __import__('re').I)
            if joined_notice:
                await self._mark_player_joined(joined_notice.group(1), channel)
        match = await get_active_match_by_bancho_channel(channel)
        if not match or _nick(sender) not in {_nick(p) for p in match['players']}:
            if not match:
                return
            match, canonical_sender = await self._canonicalize_match_player(match, sender)
            if not canonical_sender:
                return
            sender = canonical_sender
        action = match['actions'][match['action_index']]
        slot = text.strip().upper()
        if _nick(sender) != _nick(action['player']):
            logger.debug(
                "Match #%s: ignored message from %s; current turn: %s",
                match['match_id'], sender, action['player'],
            )
            return
        if slot not in match['available_slots']:
            logger.info(
                "Match #%s: invalid slot from %s rejected (available: %s)",
                match['match_id'], sender, ', '.join(match['available_slots']),
            )
            if SLOT_INPUT_PATTERN.fullmatch(slot):
                await self.irc.send_channel(
                    channel,
                    f'{slot} is unavailable. Available: {", ".join(match["available_slots"])}.',
                )
            return
        await self._handle_manual_action(match['match_id'], sender, channel, slot)

    async def _handle_manual_action(
        self, match_id: int, sender: str, channel: str, slot: str,
    ) -> None:
        """Atomically consume one valid player slot message for a match."""
        async with self._action_lock(match_id):
            # The timer or a duplicate message may have advanced the match
            # while this message waited for the lock. Always use fresh state.
            match = await get_match(match_id)
            if not match or match.get('status') != 'pickban':
                return
            action = match['actions'][match['action_index']]
            if _nick(sender) != _nick(action['player']) or slot not in match['available_slots']:
                return

            # Bancho's visible countdown is authoritative for the lobby. Cancel
            # it before handling a timely action; the next phase gets a fresh
            # `!mp timer 90` where appropriate.
            await self.irc.send_channel(channel, '!mp aborttimer')
            timeout_task = self._action_timeout_tasks.pop(match_id, None)
            if timeout_task and not timeout_task.done():
                timeout_task.cancel()
            logger.info("Match #%s: %s selected %s (%s)", match_id, sender, slot, action['kind'])
            await self._post_match_log(match_id, f"{action['kind'].title()}: {slot}.")
            history = [*match['history'], {'kind': action['kind'], 'player': action['player'], 'slot': slot}]
            next_index = match['action_index'] + 1
            status = 'waiting_ready' if action['kind'] == 'pick' else 'pickban'
            await update_match(match_id, {
                'history': history,
                'available_slots': [item for item in match['available_slots'] if item != slot],
                'action_index': next_index,
                'status': status,
                'selected_slot': slot if action['kind'] == 'pick' else None,
            })
            updated = await get_match(match_id)
            if action['kind'] == 'pick':
                await self._set_map_and_wait_ready(updated, slot)
            else:
                turn = updated['actions'][next_index]
                await self.irc.send_channel(channel, f'{slot} banned by {action["player"]}. {turn["player"]}: {turn["kind"]} a slot.')
                await self.irc.send_channel(
                    channel,
                    f'Available slots: {", ".join(updated.get("available_slots", [])) or "none"}.',
                )
                await self.irc.send_channel(channel, f'!mp timer {self.ACTION_TIMER_SECONDS}')
                self._schedule_action_timeout(match_id)
        await self._refresh_discord(match_id)

    async def _finish_played_map(self, match: dict) -> None:
        """Store a finished map, update series score, then open the next phase."""
        match = await get_match(match['match_id'])
        scores = dict(match.get('current_map_scores', {}))
        first, second = match['players']
        fallback_score_players: set[str] = set()
        # A finished map with one reported participant is a disconnect: policy
        # assigns the missing player an effective score of one.
        if first in scores and second not in scores:
            scores[second] = 1
            fallback_score_players.add(second)
        elif second in scores and first not in scores:
            scores[first] = 1
            fallback_score_players.add(first)
        if first not in scores or second not in scores:
            logger.warning("Match #%s: map ended without both results: %s", match['match_id'], scores)
            await self.irc.send_channel(match['bancho_channel'], 'Could not read both scores; the current map needs referee review.')
            return
        raw_scores = dict(scores)
        player_mods = {
            player: set(mods)
            for player, mods in match.get('current_map_player_mods', {}).items()
        }
        selected_slot = match.get('selected_slot')
        score_multipliers = {
            player: (
                1.0
                if player in fallback_score_players
                else _freemod_score_multiplier(
                    match['mode'], selected_slot, player_mods.get(player, set())
                )
            )
            for player in (first, second)
        }
        scores = {
            player: round(raw_scores[player] * score_multipliers[player])
            for player in (first, second)
        }
        for player in (first, second):
            if score_multipliers[player] != 1.0:
                logger.info(
                    "Match #%s: adjusted score for %s: %s × %s = %s",
                    match['match_id'], player, raw_scores[player], score_multipliers[player], scores[player],
                )
        logger.info(
            "Match #%s: map %s finished, result %s-%s: %s=%s, %s=%s",
            match['match_id'], match.get('selected_slot', '?'), first, second,
            first, scores[first], second, scores[second],
        )
        await self._post_match_log(
            match['match_id'],
            f"Map {match.get('selected_slot', '?')} finished: {first} {scores[first]:,} — {scores[second]:,} {second}.",
        )
        if scores[first] == scores[second]:
            slot = match.get('selected_slot')
            is_tiebreaker = bool(match.get('selected_is_tiebreaker'))
            logger.info(
                "Match #%s: tie on map %s; the map will be replayed",
                match['match_id'], slot or '?',
            )
            await self._post_match_log(match['match_id'], f"Map {slot or '?'} was tied; replaying it.", level="WARNING")
            await self.irc.send_channel(
                match['bancho_channel'],
                f"Map {slot or 'unknown'} was tied. The same map will be replayed.",
            )
            # A draw never consumes a pick or changes the series score. Apply
            # the same stored beatmap and mods again, then wait for a new ready
            # confirmation before starting the replay.
            await self._set_map_and_wait_ready(match, slot, is_tiebreaker=is_tiebreaker)
            await self._refresh_discord(match['match_id'])
            return
        winner = first if scores[first] > scores[second] else second
        series = dict(match.get('series_score', {first: 0, second: 0}))
        series[winner] = int(series.get(winner, 0)) + 1
        logger.info(
            "Match #%s: %s won the map; series score %s-%s",
            match['match_id'], winner, series[first], series[second],
        )
        await self._post_match_log(
            match['match_id'], f"{winner} won {match.get('selected_slot', '?')}; series {series[first]}-{series[second]}.",
        )
        played = [*match.get('played_maps', []), {
            'slot': match['selected_slot'], 'scores': scores, 'winner': winner,
            'raw_scores': raw_scores, 'score_multipliers': score_multipliers,
            'is_tiebreaker': bool(match.get('selected_is_tiebreaker')),
        }]
        wins_needed = match['best_of'] // 2 + 1
        if series[winner] >= wins_needed:
            logger.info("Match #%s: series won by %s", match['match_id'], winner)
            await self._post_match_log(
                match['match_id'], f"Match completed: {winner} won {series[first]}-{series[second]}.",
            )
            await update_match(match['match_id'], {'status': 'completed', 'series_score': series, 'played_maps': played})
            await self.irc.send_channel(match['bancho_channel'], f'{winner} wins the match {series[first]}-{series[second]}. GGWP!')
            self._schedule_room_close(match['match_id'], match['bancho_channel'])
        elif match.get('selected_is_tiebreaker'):
            logger.info("Match #%s: tiebreaker won by %s", match['match_id'], winner)
            await self._post_match_log(
                match['match_id'], f"Match completed on tiebreaker: {winner} won {series[first]}-{series[second]}.",
            )
            await update_match(match['match_id'], {'status': 'completed', 'series_score': series, 'played_maps': played})
            await self.irc.send_channel(match['bancho_channel'], f'{winner} wins the tiebreaker {series[first]}-{series[second]}. GGWP!')
            self._schedule_room_close(match['match_id'], match['bancho_channel'])
        elif match['action_index'] >= len(match['actions']):
            if series[first] == series[second]:
                logger.info("Match #%s: main series tied; starting TB %s", match['match_id'], match['tiebreaker_slot'])
                await self._post_match_log(
                    match['match_id'], f"Series tied {series[first]}-{series[second]}; setting tiebreaker {match['tiebreaker_slot']}.",
                )
                history = [*match['history'], {'kind': 'tiebreaker', 'player': 'automatic', 'slot': match['tiebreaker_slot']}]
                await update_match(match['match_id'], {'series_score': series, 'played_maps': played, 'history': history})
                updated = await get_match(match['match_id'])
                await self.irc.send_channel(
                    match['bancho_channel'],
                    f"Score: {first} {series[first]}-{series[second]} {second}. Tiebreaker incoming.",
                )
                await self._set_map_and_wait_ready(updated, updated['tiebreaker_slot'], is_tiebreaker=True)
            else:
                # This should only happen if no player reached the mathematical
                # match point due to an unusual external room intervention.
                await update_match(match['match_id'], {'status': 'completed', 'series_score': series, 'played_maps': played})
                await self._post_match_log(
                    match['match_id'], f"Match completed: final score {series[first]}-{series[second]}.",
                )
        else:
            await update_match(match['match_id'], {'status': 'pickban', 'series_score': series, 'played_maps': played, 'current_map_scores': {}})
            updated = await get_match(match['match_id'])
            turn = updated['actions'][updated['action_index']]
            await self.irc.send_channel(
                match['bancho_channel'],
                f'Score: {first} {series[first]}-{series[second]} {second}. '
                f'{turn["player"]}: {turn["kind"]} a slot.',
            )
            await self.irc.send_channel(
                match['bancho_channel'],
                f'Available slots: {", ".join(updated.get("available_slots", [])) or "none"}.',
            )
            await self.irc.send_channel(match['bancho_channel'], f'!mp timer {self.ACTION_TIMER_SECONDS}')
            self._schedule_action_timeout(match['match_id'])
        await self._refresh_discord(match['match_id'])

    def _schedule_room_close(self, match_id: int, channel: str) -> None:
        """Close a finished MP room after a one-minute farewell period."""
        existing = self._close_tasks.get(match_id)
        if existing and not existing.done():
            return
        self._close_tasks[match_id] = asyncio.create_task(self._close_room_after_delay(match_id, channel))

    async def _close_room_after_delay(self, match_id: int, channel: str) -> None:
        try:
            logger.info("Match #%s: lobby will close in 60 seconds", match_id)
            await asyncio.sleep(60)
            await self.irc.send_channel(channel, "Closing the lobby in 5 seconds. Thanks for playing!")
            await asyncio.sleep(5)
            await self.irc.send_channel(channel, "!mp close")
            self.irc.forget_channel(channel)
            logger.info("Match #%s: sent !mp close", match_id)
            await self._post_match_log(match_id, "Sent !mp close after the farewell period.")
        except asyncio.CancelledError:
            logger.info("Match #%s: close timer cancelled", match_id)
        except Exception:
            logger.exception("Match #%s: failed to close the MP lobby", match_id)
        finally:
            self._close_tasks.pop(match_id, None)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OsuCommands(bot))
