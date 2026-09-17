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


class MultiplayerMod(str, Enum):
    """Tokens used to compose the enforced multiplayer mod combinations."""
    NO_FAIL = "nf"
    HIDDEN = "hd"
    HARD_ROCK = "hr"
    DOUBLE_TIME = "dt"
    FREE_MOD = "freemod"

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
    category = get_ruleset(mode).category_from_slot(slot)
    variants = {
        "hd": (MultiplayerMod.NO_FAIL, MultiplayerMod.HIDDEN),
        "hr": (MultiplayerMod.NO_FAIL, MultiplayerMod.HARD_ROCK),
        "dt": (MultiplayerMod.NO_FAIL, MultiplayerMod.DOUBLE_TIME),
        "fm": (MultiplayerMod.NO_FAIL, MultiplayerMod.FREE_MOD),
        "tb": (MultiplayerMod.NO_FAIL, MultiplayerMod.FREE_MOD),
    }
    return " ".join(item.value for item in variants.get(category, (MultiplayerMod.NO_FAIL,)))

def _mod_tokens(value: str) -> set[str]:
    aliases = {
        'nofail': 'nf', 'no fail': 'nf', 'hidden': 'hd',
        'hardrock': 'hr', 'hard rock': 'hr', 'doubletime': 'dt',
        'double time': 'dt', 'freemod': 'freemod', 'free mod': 'freemod',
    }
    return {aliases.get(part.strip().casefold(), part.strip().casefold())
            for part in re.split(r'[,|+ ]+', value) if part.strip()}

class OsuCommands(commands.Cog, name="osu! multiplayer"):
    ACTION_TIMER_SECONDS = 90
    READY_MESSAGES = {'all players are ready', 'all players ready'}
    MATCH_LOG_CHANNEL_ID = 1550058586846011473

    def __init__(self, bot: commands.Bot) -> None:
        self.bot, self.irc = bot, BanchoIRC()
        self.irc.message_handler = self._on_bancho_message
        self.irc.private_message_handler = self._on_bancho_private_message
        self.irc.join_handler = self._on_bancho_join
        self.irc.reconnect_handler = self._on_irc_reconnected
        if self.irc.configured:
            logger.info(
                "Bancho IRC: клиент инициализирован; автоподключение при старте отключено, "
                "подключение выполняется по /osu-connect или /match_create"
            )
        else:
            logger.warning(
                "Bancho IRC: клиент инициализирован, но не запустится — "
                "BANCHO_USERNAME или BANCHO_IRC_PASSWORD не заданы"
            )
        self._close_tasks: dict[int, asyncio.Task] = {}
        self._invite_tasks: dict[int, asyncio.Task] = {}
        self._action_timeout_tasks: dict[int, asyncio.Task] = {}
        self._settings_tasks: dict[int, asyncio.Task] = {}
        self._action_locks: dict[int, asyncio.Lock] = {}

    def cog_unload(self) -> None:
        # discord.py calls cog_unload synchronously; do not leave an unawaited
        # coroutine behind when the extension is reloaded or the bot exits.
        for task in self._close_tasks.values():
            task.cancel()
        for task in self._invite_tasks.values():
            task.cancel()
        for task in self._action_timeout_tasks.values():
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
                logger.warning("Матч #%s: канал логов Discord недоступен", match_id)
                return
            timestamp = datetime.now().strftime("%H:%M:%S")
            await channel.send(
                f"```text\n[{timestamp}] [{level}] Match #{match_id}: {message[:1800]}\n```",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            logger.warning("Матч #%s: не удалось отправить событие в Discord-логи", match_id)

    async def _refresh_match_log_embed(self, match: dict) -> None:
        """Keep one staff-channel embed synchronized with the public match output."""
        try:
            channel = await self._get_discord_channel(self.MATCH_LOG_CHANNEL_ID)
            if channel is None:
                logger.warning("Матч #%s: канал логов Discord недоступен", match["match_id"])
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
            logger.warning("Матч #%s: не удалось обновить embed в Discord-логах", match["match_id"])

    async def _send_match_pool(self, match: dict) -> None:
        """Post the exact stored pool to the match's Discord channel once."""
        if match.get("pool_message_id"):
            return
        try:
            channel = await self._get_discord_channel(int(match["discord_channel_id"]))
            pool_cog = self.bot.get_cog("Команды пулов")
            if channel is None or pool_cog is None:
                raise RuntimeError("канал матча или cog пулов недоступен")
            embed, pool = await pool_cog._pool_view_embed(int(match["pool_id"]))
            if embed is None or pool is None:
                raise RuntimeError("пул матча не найден")
            embed.title = f"🗺️ Пул матча: {pool['name']}"
            embed.set_footer(text=f"Режим: {pool['mode'].upper()} · BO{match['best_of']}")
            message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await update_match(match["match_id"], {"pool_message_id": message.id})
            await self._post_match_log(match["match_id"], f"Pool posted to the match channel: {pool['name']}.")
        except Exception:
            logger.exception("Матч #%s: не удалось опубликовать пул в Discord", match["match_id"])
            await self._post_match_log(
                match["match_id"], "Could not post the pool to the match channel.", level="ERROR",
            )

    async def restore_live_matches(self) -> None:
        """Rejoin and resume persisted live rooms after a bot process restart."""
        matches = await get_live_matches()
        if not matches:
            logger.info("Bancho IRC: активных матчей для восстановления нет")
            return
        if not self.irc.configured:
            logger.error("Bancho IRC: невозможно восстановить %s матчей — IRC не настроен", len(matches))
            return
        logger.info("Bancho IRC: восстанавливаю активные матчи: %s", len(matches))
        for match in matches:
            try:
                await self.irc.restore_channel(match['bancho_channel'])
            except Exception:
                logger.exception("Матч #%s: не удалось восстановить MP-комнату", match['match_id'])

    @staticmethod
    def _embed(match: dict) -> discord.Embed:
        text = (
            f"Комната: [#{match['bancho_match_id']}](https://osu.ppy.sh/community/matches/{match['bancho_match_id']})\n"
            f"Пул: **{match['pool_name']}** · `{match['mode'].upper()}` · BO{match['best_of']}"
        )
        # Do not reveal the roll in Discord until both players are confirmed
        # inside the Bancho lobby and pick/ban is actually underway.
        if match['status'] != 'waiting_players':
            text += (
                f"\nРолл: **{match['roll_winner']}** выбирает первым; "
                f"**{match['roll_loser']}** банит первым."
            )
        if match['history']:
            text += "\n\n**Выборы:** " + " · ".join(
                f"{item['slot']} ({'TB' if item['kind'] == 'tiebreaker' else 'бан' if item['kind'] == 'ban' else 'пик'}: {item['player']})"
                for item in match['history']
            )
        embed = discord.Embed(title=f"🎮 Матч #{match['match_id']}", description=text, color=0x5865F2)
        players = match['players']
        series = match.get('series_score', {})
        embed.add_field(
            name="Счёт серии",
            value=f"**{players[0]}** `{series.get(players[0], 0)}` — `{series.get(players[1], 0)}` **{players[1]}**",
            inline=False,
        )
        played_maps = match.get('played_maps', [])
        if played_maps:
            results = []
            for number, played in enumerate(played_maps, start=1):
                scores = played.get('scores', {})
                first_score = scores.get(players[0], '?')
                second_score = scores.get(players[1], '?')
                label = "TB" if played.get('is_tiebreaker') else played['slot']
                results.append(
                    f"`#{number} {label}` — **{players[0]}** `{first_score:,}` : `{second_score:,}` **{players[1]}** · победил **{played['winner']}**"
                    if isinstance(first_score, int) and isinstance(second_score, int)
                    else f"`#{number} {label}` — результаты: `{first_score}` : `{second_score}`"
                )
            embed.add_field(name="Результаты карт", value="\n".join(results)[-1024:], inline=False)
        if match['status'] == 'waiting_players':
            joined = match.get('joined_players', [])
            embed.add_field(
                name="Ожидание игроков",
                value=f"Подключились: `{', '.join(joined) if joined else 'пока никто'}`\n"
                      "Roll начнётся после входа обоих игроков в lobby.",
                inline=False,
            )
        elif match['status'] == 'pickban':
            action = match['actions'][match['action_index']]
            embed.add_field(
                name="Текущий ход — в игре",
                value=f"**{action['player']}**: слот для **{'бана' if action['kind'] == 'ban' else 'пика'}** пишется в чате лобби.\n"
                      f"Доступно: `{', '.join(match['available_slots'])}`",
                inline=False,
            )
        elif match['status'] == 'waiting_ready':
            embed.add_field(
                name="Ожидание готовности",
                value=f"Карта `{match.get('selected_slot', '?')}` выставлена. Бот ждёт `All players are ready` в MP-чате.",
                inline=False,
            )
        elif match['status'] == 'checking_settings':
            embed.add_field(name="Проверка лобби", value="Бот проверяет карту, режим и моды перед стартом.", inline=False)
        elif match['status'] == 'game_running':
            embed.add_field(
                name="Карта запущена",
                value=f"Играется `{match.get('selected_slot', '?')}`.",
                inline=False,
            )
        elif match['status'] == 'completed':
            winner = max(series, key=series.get) if series else '—'
            embed.add_field(name="Матч завершён", value=f"Победитель: **{winner}**.", inline=False)
        elif match['status'] == 'cancelled':
            embed.add_field(
                name="Матч отменён",
                value="Не оба игрока присоединились к лобби за отведённые 5 минут.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Статус матча",
                value=f"Текущее состояние: `{match.get('status', 'unknown')}`.",
                inline=False,
            )
        return embed

    async def pool_name_autocomplete(self, interaction: discord.Interaction, current: str):
        from database import list_pools
        pools = await list_pools(status='ranked')
        return [app_commands.Choice(name=p['name'][:100], value=p['name'][:100]) for p in pools if current.casefold() in p['name'].casefold()][:25]

    @app_commands.command(name='osu-connect', description='Привязать osu! аккаунт через код в личных сообщениях')
    async def osu_connect(self, interaction: discord.Interaction):
        """Start a one-time Discord <-> osu! account ownership challenge."""
        if not self.irc.configured:
            await interaction.response.send_message(
                '❌ Bancho IRC не настроен. Добавь BANCHO_USERNAME и BANCHO_IRC_PASSWORD в приватный .env.',
                ephemeral=True,
            )
            return

        existing = await get_osu_account_by_discord(interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"✅ Уже подключён osu! аккаунт **{existing['osu_username']}**.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        code = _new_osu_login_code()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        if not await create_osu_login_challenge(
            interaction.user.id, _hash_osu_login_code(code), expires_at
        ):
            await interaction.followup.send('❌ Не удалось создать код подключения. Повтори попытку позже.', ephemeral=True)
            return

        self.irc.set_keep_connected(True)
        try:
            await self.irc.connect()
        except Exception:
            await delete_osu_login_challenge(interaction.user.id)
            logger.exception("Не удалось подключить Bancho IRC для osu-connect")
            await interaction.followup.send(
                '❌ Не удалось подключиться к Bancho IRC. Проверь сеть и попробуй ещё раз.',
                ephemeral=True,
            )
            return

        logger.info("Bancho IRC: соединение активно, ожидаю код подтверждения osu-connect")
        bot_name = self.irc.username.replace('_', ' ')
        await interaction.followup.send(
            f"Открой osu! и отправь в PM аккаунту **{bot_name}** только этот код:\n"
            f"`{code}`\n\nКод действует 10 минут и одноразовый.",
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
        logger.info("osu-connect: получен корректный код от osu! пользователя %s", osu_username)
        try:
            profile = await osu_manager.get_user(osu_username)
        except Exception:
            logger.warning("osu-connect: не удалось подтвердить osu! отправителя %s", osu_username)
            try:
                await self.irc.send_private_message(
                    sender,
                    'Не удалось подтвердить этот osu! аккаунт. Проверь доступность профиля и отправь код ещё раз.',
                )
            except Exception:
                logger.exception("osu-connect: не удалось отправить ответ пользователю %s", osu_username)
            return

        status, account = await complete_osu_login(
            code_hash, profile['id'], profile['username']
        )
        logger.info("osu-connect: результат привязки для %s: %s", profile['username'], status)
        messages = {
            'linked': f"✅ osu! аккаунт {profile['username']} успешно подключён к Discord.",
            'already_linked': f"✅ osu! аккаунт {profile['username']} уже был подключён к этому Discord.",
            'osu_already_linked': '❌ Этот osu! аккаунт уже подключён к другому Discord-аккаунту.',
            'discord_already_linked': '❌ У этого Discord-аккаунта уже подключён другой osu! аккаунт.',
            'storage_error': '❌ Не удалось сохранить подключение. Повтори попытку через несколько секунд.',
        }
        try:
            await self.irc.send_private_message(sender, messages.get(status, '❌ Не удалось завершить подключение.'))
        except Exception:
            logger.exception("osu-connect: не удалось отправить результат в PM %s", osu_username)

        if status in {'linked', 'already_linked'} and account:
            try:
                discord_user = self.bot.get_user(int(account['discord_user_id']))
                if discord_user is None:
                    discord_user = await self.bot.fetch_user(int(account['discord_user_id']))
                await discord_user.send(f"✅ osu! аккаунт **{profile['username']}** успешно подключён.")
            except discord.DiscordException:
                logger.warning("osu-connect: не удалось отправить Discord DM пользователю %s", account['discord_user_id'])

    async def _refresh_linked_account_username(self, account: dict) -> dict:
        """Refresh a linked account's cached username before match registration."""
        osu_user_id = int(account['osu_user_id'])
        profile = await osu_manager.get_user_by_id(osu_user_id)
        if int(profile['id']) != osu_user_id:
            raise RuntimeError(f"osu! API вернул неожиданный ID для аккаунта {osu_user_id}")
        username = str(profile.get('username', '')).strip()
        if not username:
            raise RuntimeError(f"osu! API не вернул имя для аккаунта {osu_user_id}")

        old_username = str(account.get('osu_username', '')).strip()
        if old_username != username:
            if await update_osu_account_username(osu_user_id, username):
                logger.info(
                    "match_create: обновлено имя osu! ID %s перед матчем: %s -> %s",
                    osu_user_id, old_username or '<пусто>', username,
                )
            else:
                logger.warning(
                    "match_create: актуальное имя osu! ID %s получено (%s), но MongoDB не обновлена",
                    osu_user_id, username,
                )
        return {**account, 'osu_username': username}

    @app_commands.command(name='match_create', description='Создать Bancho-лобби и начать pick/ban в игре')
    @app_commands.describe(player_one='Первый участник сервера с привязанным osu! аккаунтом', player_two='Второй участник сервера с привязанным osu! аккаунтом', pool_name='Ranked-пул', format='Формат')
    @app_commands.choices(format=[app_commands.Choice(name='BO5 — 1 бан', value='5:1'), app_commands.Choice(name='BO7 — 1 бан', value='7:1'), app_commands.Choice(name='BO7 — 2 бана', value='7:2'), app_commands.Choice(name='BO9 — 2 бана', value='9:2')])
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
                '❌ Создавать матч можно только на сервере Discord.', ephemeral=True,
            )
            return
        creator_account = await get_osu_account_by_discord(interaction.user.id)
        if not creator_account or not str(creator_account.get('osu_username', '')).strip():
            logger.info("match_create отклонён: Discord ID %s не имеет привязанного osu! аккаунта", interaction.user.id)
            await interaction.response.send_message(
                '❌ Сначала привяжи свой osu! аккаунт командой `/osu-connect`.', ephemeral=True,
            )
            return
        if not self.irc.configured:
            await interaction.response.send_message('❌ Добавь BANCHO_USERNAME и BANCHO_IRC_PASSWORD в приватный .env, затем перезапусти бота.', ephemeral=True); return
        if player_one.id == player_two.id:
            await interaction.response.send_message('❌ Укажи двух разных участников сервера.', ephemeral=True); return
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
                "match_create отклонён: не привязаны osu! аккаунты участников %s",
                ', '.join(str(member.id) for member, account in (
                    (player_one, player_one_account), (player_two, player_two_account)
                ) if not account or not str(account.get('osu_username', '')).strip()),
            )
            await interaction.response.send_message(
                f"❌ Участники {', '.join(missing_accounts)} должны сначала привязать osu! аккаунт командой `/osu-connect`.",
                ephemeral=True,
            )
            return
        active_player_match = await get_active_match_for_osu_users([
            int(player_one_account['osu_user_id']),
            int(player_two_account['osu_user_id']),
        ])
        if active_player_match:
            await interaction.response.send_message(
                f"❌ Один из выбранных игроков уже участвует в активном матче "
                f"#{active_player_match['match_id']}.",
                ephemeral=True,
            )
            return
        pools = [p for p in await get_pool_by_name(pool_name) if p['status'] == 'ranked']
        if len(pools) != 1:
            await interaction.response.send_message('❌ Ranked-пул с таким названием не найден.', ephemeral=True); return
        pool = pools[0]; best_of, bans = map(int, format.value.split(':'))
        slots = _ordered_slots(pool['maps'], pool['mode'])
        tiebreaker = next((item['slot'].upper() for item in pool['maps'] if item['slot'].upper() == 'TB'), None)
        required_slots = (best_of - 1) + 2 * bans
        if not tiebreaker:
            await interaction.response.send_message('❌ В Ranked-пуле нет обязательной карты `TB`.', ephemeral=True); return
        if len(slots) < required_slots:
            await interaction.response.send_message('❌ В пуле недостаточно слотов без TB для этого формата.', ephemeral=True); return
        await interaction.response.defer(thinking=True)
        try:
            player_one_account = await self._refresh_linked_account_username(player_one_account)
            player_two_account = await self._refresh_linked_account_username(player_two_account)
        except Exception:
            logger.exception("match_create: не удалось обновить osu!-имена участников по ID")
            await interaction.followup.send(
                '❌ Не удалось проверить актуальные osu!-имена участников. Повтори попытку позже.',
                ephemeral=True,
            )
            return
        player_one_name = str(player_one_account['osu_username']).strip()
        player_two_name = str(player_two_account['osu_username']).strip()
        if _nick(player_one_name) == _nick(player_two_name):
            logger.warning(
                "match_create отклонён после обновления имён: Discord ID %s и %s связаны с одним osu! аккаунтом",
                player_one.id, player_two.id,
            )
            await interaction.followup.send(
                '❌ У выбранных участников не могут быть привязаны один и тот же osu! аккаунт.',
                ephemeral=True,
            )
            return
        try:
            logger.info(
                "Создание матча: %s (Discord ID %s) vs %s (Discord ID %s), пул «%s», %s",
                player_one_name, player_one.id, player_two_name, player_two.id, pool['name'], format.name,
            )
            channel = await self.irc.make_match(f'Ladder test — {player_one_name} vs {player_two_name}')
        except Exception as error:
            logger.exception("Не удалось создать Bancho-лобби")
            await interaction.followup.send(f'❌ Не удалось создать Bancho-лобби: {error}', ephemeral=True); return
        winner, loser = (player_one_name, player_two_name) if random.choice((True, False)) else (player_two_name, player_one_name)
        map_choices = {
            item['slot'].upper(): {
                'beatmap_id': item['beatmap_id'],
                'mods': _multiplayer_mods(item['slot'], pool['mode']),
            }
            for item in pool['maps']
        }
        match_id = await create_match({'status':'waiting_players', 'discord_channel_id':interaction.channel_id, 'discord_message_id':None, 'pool_message_id':None, 'match_log_message_id':None, 'bancho_channel':channel, 'bancho_match_id':channel.removeprefix('#mp_'), 'pool_id':pool['pool_id'], 'pool_name':pool['name'], 'mode':pool['mode'], 'players':[player_one_name, player_two_name], 'player_osu_ids':[int(player_one_account['osu_user_id']), int(player_two_account['osu_user_id'])], 'joined_players':[], 'join_deadline':datetime.now(timezone.utc) + timedelta(minutes=5), 'roll_winner':winner, 'roll_loser':loser, 'best_of':best_of, 'bans_per_player':bans, 'actions':_actions(best_of,bans,loser,winner), 'action_index':0, 'available_slots':slots, 'tiebreaker_slot':tiebreaker, 'map_choices':map_choices, 'history':[]})
        logger.info("Матч #%s сохранён: %s, roll winner=%s", match_id, channel, winner)
        await self._post_match_log(
            match_id,
            f"Lobby {channel} created: {player_one_name} vs {player_two_name}; "
            f"pool {pool['name']}; BO{best_of}.",
        )
        await self.irc.send_channel(channel, 'Waiting for both players to join before the roll.')
        match = await get_match(match_id)
        await interaction.followup.send(embed=self._embed(match))
        message = await interaction.original_response()
        await update_match(match_id, {'discord_message_id': message.id})
        await self._refresh_match_log_embed(await get_match(match_id))
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
                logger.info("Матч #%s: раунд инвайтов %s, осталось %.0f с", match_id, attempt, seconds_left)
                await asyncio.sleep(min(60, seconds_left))
            logger.info("Матч #%s: пятиминутное окно входа завершено", match_id)
            match = await get_match_by_bancho_channel(channel)
            if match and match['status'] == 'waiting_players':
                joined = match.get('joined_players', [])
                await update_match(match_id, {'status': 'cancelled'})
                logger.info("Матч #%s: отменён — в лобби вошли не все игроки (%s/2)", match_id, len(joined))
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
            logger.info("Матч #%s: повторные инвайты остановлены — оба игрока вошли", match_id)
        except Exception:
            logger.exception("Матч #%s: ошибка повторной отправки инвайтов", match_id)
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
                "Матч #%s: имя %s не сопоставлено с участниками по osu! ID",
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
        updates['played_maps'] = deepcopy(match.get('played_maps', []))
        for played in updates['played_maps']:
            if 'winner' in played:
                played['winner'] = replace_name(played['winner'])
            if isinstance(played.get('scores'), dict):
                played['scores'] = {
                    replace_name(name): score
                    for name, score in played['scores'].items()
                }

        if not await update_osu_account_username(osu_user_id, canonical):
            logger.warning(
                "Матч #%s: имя osu! ID %s обновлено только в матче, MongoDB не обновлена",
                match.get('match_id', '?'), osu_user_id,
            )
        await update_match(match['match_id'], updates)
        match = {**match, **updates}
        logger.info(
            "Матч #%s: обнаружено переименование osu! ID %s: %s -> %s",
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
        logger.info("Матч #%s: %s вошёл в лобби (%s/2)", match['match_id'], canonical, len(joined))
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
        logger.info("Матч #%s: IRC восстановлен, состояние=%s", match_id, match['status'])
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
            logger.exception("Матч #%s: не удалось восстановить состояние после reconnect", match_id)

    async def _refresh_discord(self, match_id: int) -> None:
        match = await get_match(match_id)
        if not match:
            return
        if match.get('discord_message_id'):
            try:
                channel = await self._get_discord_channel(match['discord_channel_id'])
                if channel:
                    message = await channel.fetch_message(match['discord_message_id'])
                    await message.edit(embed=self._embed(match))
            except discord.DiscordException:
                logger.warning("Матч #%s: не удалось обновить Discord-сообщение", match_id)
        await self._refresh_match_log_embed(match)

    async def _set_map_and_wait_ready(self, match: dict, slot: str, *, is_tiebreaker: bool = False) -> None:
        """Apply one stored pool map, then wait for BanchoBot readiness."""
        choice = match['map_choices'][slot]
        mode_id = {'std': 0, 'taiko': 1, 'ctb': 2, 'mania': 3, 'mania4k': 3, 'mania7k': 3}[match['mode']]
        label = 'tiebreaker' if is_tiebreaker else 'picked map'
        logger.info(
            "Матч #%s: установка %s %s (beatmap %s, режим %s, mods %s)",
            match['match_id'], label, slot, choice['beatmap_id'], mode_id, choice['mods'],
        )
        await self._post_match_log(
            match['match_id'],
            f"Set {label} {slot}: beatmap {choice['beatmap_id']}, mode {mode_id}, mods {choice['mods']}.",
        )
        await update_match(match['match_id'], {
            'status': 'waiting_ready', 'selected_slot': slot, 'selected_is_tiebreaker': is_tiebreaker,
            'current_map_scores': {},
        })
        await self.irc.send_channel(match['bancho_channel'], f"!mp map {choice['beatmap_id']} {mode_id}")
        await self.irc.send_channel(match['bancho_channel'], f"!mp mods {choice['mods']}")
        await self.irc.send_channel(match['bancho_channel'], 'Map is set. Waiting for: All players are ready.')
        await self.irc.send_channel(match['bancho_channel'], f'!mp timer {self.ACTION_TIMER_SECONDS}')

    async def _queue_settings_check(self, match_id: int, channel: str) -> bool:
        """Queue the mandatory settings check for one fresh ready event."""
        match = await get_match(match_id)
        if not match or match.get('status') != 'waiting_ready':
            return False
        selected = match.get('selected_slot', 'the selected map')
        logger.info("Матч #%s: все игроки готовы для %s; запрашиваем !mp settings", match_id, selected)
        await self._post_match_log(match_id, f"Both players are ready for {selected}; requesting lobby settings.")
        await update_match(match_id, {'status': 'checking_settings'})
        old = self._settings_tasks.pop(match_id, None)
        if old and not old.done():
            old.cancel()
        self._settings_tasks[match_id] = asyncio.create_task(
            self._check_settings_and_start(match_id, channel)
        )
        return True

    async def _check_settings_and_start(self, match_id: int, channel: str) -> None:
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
                "Матч #%s: !mp settings получены: карта=%s, активные моды=%s, игроков=%s, готовых=%s",
                match_id,
                settings.get('beatmap_id', '?'),
                settings.get('active_mods', 'None'),
                settings.get('player_count', 0),
                sum(player.get('status', '').casefold() == 'ready' for player in settings.get('players', [])),
            )
            errors: list[str] = []
            if settings.get('beatmap_id') != choice.get('beatmap_id'):
                errors.append(f"карта {settings.get('beatmap_id', '?')} вместо {choice.get('beatmap_id', '?')}")
            required = _mod_tokens(choice.get('mods', ''))
            active = _mod_tokens(settings.get('active_mods', ''))
            if 'freemod' in required:
                # NoFail is a per-player mod in FreeMod rooms, not a global
                # room mod reported by `Active mods`.
                if active != {'freemod'}:
                    errors.append(f"глобальные моды {settings.get('active_mods', 'None')} вместо FreeMod")
                for player in settings.get('players', []):
                    if 'nf' not in _mod_tokens(','.join(player.get('mods', []))):
                        errors.append(f"у игрока {player.get('username', '?')} не включён NoFail")
            elif active != required:
                errors.append(f"моды {settings.get('active_mods', 'None')} вместо {choice.get('mods', 'nf')}")
            if settings.get('player_count') != 2 or len(settings.get('players', [])) < 2:
                errors.append('в лобби не подтверждены оба игрока')
            else:
                expected_ids = {int(user_id) for user_id in match.get('player_osu_ids', [])}
                actual_ids = {int(player['user_id']) for player in settings.get('players', [])}
                if expected_ids and actual_ids != expected_ids:
                    errors.append('в лобби находятся не те участники матча')
            if len(settings.get('players', [])) >= 2 and any(player.get('status', '').casefold() != 'ready' for player in settings.get('players', [])):
                errors.append('не все игроки имеют статус Ready')
            if errors:
                logger.warning("Матч #%s: проверка !mp settings не пройдена: %s", match_id, '; '.join(errors))
                await self._post_match_log(
                    match_id, f"Lobby validation failed for {selected}: {'; '.join(errors)}.", level="WARNING",
                )
                await update_match(match_id, {'status': 'waiting_ready'})
                await self.irc.send_channel(channel, 'Lobby check failed: ' + '; '.join(errors) + '. Map/mods will be reapplied.')
                await self._set_map_and_wait_ready(await get_match(match_id), selected)
            else:
                logger.info("Матч #%s: !mp settings подтверждены, запускаем карту %s", match_id, selected)
                await self._post_match_log(match_id, f"Lobby validation passed for {selected}; starting the map.")
                await update_match(match_id, {'status': 'game_running'})
                await self.irc.send_channel(channel, '!mp aborttimer')
                await self.irc.send_channel(channel, '!mp start 5')
            await self._refresh_discord(match_id)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Матч #%s: ошибка проверки !mp settings", match_id)
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
                        "Матч #%s: таймаут хода %s — бан пропущен (%s)",
                        match_id, action['player'], action['kind'],
                    )
                    await self.irc.send_channel(match['bancho_channel'], f"{action['player']} did not ban in time. Ban skipped.")
                    await self._post_match_log(match_id, f"{action['player']} did not ban in time; ban skipped.", level="WARNING")
                    await self._advance_action(match, None, automatic=True)
                else:
                    if not match['available_slots']:
                        logger.error("Матч #%s: таймаут пика, но доступных слотов не осталось", match_id)
                        return
                    slot = random.SystemRandom().choice(match['available_slots'])
                    logger.info("Матч #%s: таймаут хода %s — псевдослучайный пик %s", match_id, action['player'], slot)
                    await self.irc.send_channel(match['bancho_channel'], f"{action['player']} did not pick in time. Pseudo-random pick: {slot}.")
                    await self._post_match_log(
                        match_id, f"{action['player']} did not pick in time; pseudo-random pick {slot}.", level="WARNING",
                    )
                    await self._advance_action(match, slot, automatic=True)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Матч #%s: ошибка обработки таймаута действия", match_id)
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
            "Матч #%s: ход %s %s%s обработан, следующий статус=%s (%s/%s)",
            match['match_id'],
            action['kind'],
            slot or 'пропущен',
            ' автоматически' if automatic else '',
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
            logger.info("Матч #%s: BanchoBot сообщил, что игроки готовы", live_match['match_id'])
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
                    logger.info("Матч #%s: результат %s = %s", live_match['match_id'], player, scores[player])
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
                "Матч #%s: сообщение игрока %s проигнорировано, сейчас ход %s",
                match['match_id'], sender, action['player'],
            )
            return
        if slot not in match['available_slots']:
            logger.info(
                "Матч #%s: недопустимый слот от %s отклонён (доступно: %s)",
                match['match_id'], sender, ', '.join(match['available_slots']),
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
            logger.info("Матч #%s: %s выбрал %s (%s)", match_id, sender, slot, action['kind'])
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
                await self.irc.send_channel(channel, f'!mp timer {self.ACTION_TIMER_SECONDS}')
                self._schedule_action_timeout(match_id)
        await self._refresh_discord(match_id)

    async def _finish_played_map(self, match: dict) -> None:
        """Store a finished map, update series score, then open the next phase."""
        match = await get_match(match['match_id'])
        scores = dict(match.get('current_map_scores', {}))
        first, second = match['players']
        # A finished map with one reported participant is a disconnect: policy
        # assigns the missing player an effective score of one.
        if first in scores and second not in scores:
            scores[second] = 1
        elif second in scores and first not in scores:
            scores[first] = 1
        if first not in scores or second not in scores:
            logger.warning("Матч #%s: завершение карты без обоих результатов: %s", match['match_id'], scores)
            await self.irc.send_channel(match['bancho_channel'], 'Could not read both scores; the current map needs referee review.')
            return
        logger.info(
            "Матч #%s: карта %s завершена, результат %s-%s: %s=%s, %s=%s",
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
                "Матч #%s: ничья на карте %s; карта будет переиграна",
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
            "Матч #%s: %s выиграл карту, счёт серии %s-%s",
            match['match_id'], winner, series[first], series[second],
        )
        await self._post_match_log(
            match['match_id'], f"{winner} won {match.get('selected_slot', '?')}; series {series[first]}-{series[second]}.",
        )
        played = [*match.get('played_maps', []), {
            'slot': match['selected_slot'], 'scores': scores, 'winner': winner,
            'is_tiebreaker': bool(match.get('selected_is_tiebreaker')),
        }]
        wins_needed = match['best_of'] // 2 + 1
        if series[winner] >= wins_needed:
            logger.info("Матч #%s: серия завершена победой %s", match['match_id'], winner)
            await self._post_match_log(
                match['match_id'], f"Match completed: {winner} won {series[first]}-{series[second]}.",
            )
            await update_match(match['match_id'], {'status': 'completed', 'series_score': series, 'played_maps': played})
            await self.irc.send_channel(match['bancho_channel'], f'{winner} wins the match {series[first]}-{series[second]}. GGWP!')
            self._schedule_room_close(match['match_id'], match['bancho_channel'])
        elif match.get('selected_is_tiebreaker'):
            logger.info("Матч #%s: тайбрейкер завершён победой %s", match['match_id'], winner)
            await self._post_match_log(
                match['match_id'], f"Match completed on tiebreaker: {winner} won {series[first]}-{series[second]}.",
            )
            await update_match(match['match_id'], {'status': 'completed', 'series_score': series, 'played_maps': played})
            await self.irc.send_channel(match['bancho_channel'], f'{winner} wins the tiebreaker {series[first]}-{series[second]}. GGWP!')
            self._schedule_room_close(match['match_id'], match['bancho_channel'])
        elif match['action_index'] >= len(match['actions']):
            if series[first] == series[second]:
                logger.info("Матч #%s: основная серия завершилась вничью, запускается TB %s", match['match_id'], match['tiebreaker_slot'])
                await self._post_match_log(
                    match['match_id'], f"Series tied {series[first]}-{series[second]}; setting tiebreaker {match['tiebreaker_slot']}.",
                )
                history = [*match['history'], {'kind': 'tiebreaker', 'player': 'automatic', 'slot': match['tiebreaker_slot']}]
                await update_match(match['match_id'], {'series_score': series, 'played_maps': played, 'history': history})
                updated = await get_match(match['match_id'])
                await self.irc.send_channel(match['bancho_channel'], f"Series is tied {series[first]}-{series[second]}. {updated['tiebreaker_slot']} is the tiebreaker.")
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
            await self.irc.send_channel(match['bancho_channel'], f'Score: {first} {series[first]}-{series[second]} {second}. {turn["player"]}: {turn["kind"]} a slot.')
        await self._refresh_discord(match['match_id'])

    def _schedule_room_close(self, match_id: int, channel: str) -> None:
        """Close a finished MP room after a one-minute farewell period."""
        existing = self._close_tasks.get(match_id)
        if existing and not existing.done():
            return
        self._close_tasks[match_id] = asyncio.create_task(self._close_room_after_delay(match_id, channel))

    async def _close_room_after_delay(self, match_id: int, channel: str) -> None:
        try:
            logger.info("Матч #%s: лобби будет закрыто через 60 секунд", match_id)
            await asyncio.sleep(60)
            await self.irc.send_channel(channel, "Closing the lobby in 5 seconds. Thanks for playing!")
            await asyncio.sleep(5)
            await self.irc.send_channel(channel, "!mp close")
            self.irc.forget_channel(channel)
            logger.info("Матч #%s: отправлена команда !mp close", match_id)
            await self._post_match_log(match_id, "Sent !mp close after the farewell period.")
        except asyncio.CancelledError:
            logger.info("Матч #%s: таймер закрытия отменён", match_id)
        except Exception:
            logger.exception("Матч #%s: не удалось закрыть MP-лобби", match_id)
        finally:
            self._close_tasks.pop(match_id, None)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OsuCommands(bot))
