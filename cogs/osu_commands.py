"""Bancho IRC match flow; IRC chat is restricted to BanchoBot and the MP room."""
from __future__ import annotations
import asyncio
import random
import logging
import re
from enum import Enum
import discord
from discord import app_commands
from discord.ext import commands
from bancho_irc import BanchoIRC
from database import (
    create_match, get_match, get_pool_by_name, update_match,
    get_active_match_by_bancho_channel, get_match_by_bancho_channel,
)
from rulesets import get_ruleset

logger = logging.getLogger("osu_tourney.matches")


class MultiplayerMod(str, Enum):
    """Tokens used to compose the enforced multiplayer mod combinations."""
    NO_FAIL = "nf"
    HIDDEN = "hd"
    HARD_ROCK = "hr"
    DOUBLE_TIME = "dt"
    FREE_MOD = "freemod"

def _nick(value: str) -> str:
    return value.strip().strip('[]').replace('_', ' ').casefold()

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

    def __init__(self, bot: commands.Bot) -> None:
        self.bot, self.irc = bot, BanchoIRC()
        self.irc.message_handler = self._on_bancho_message
        self.irc.join_handler = self._on_bancho_join
        self._close_tasks: dict[int, asyncio.Task] = {}
        self._invite_tasks: dict[int, asyncio.Task] = {}
        self._action_timeout_tasks: dict[int, asyncio.Task] = {}
        self._settings_tasks: dict[int, asyncio.Task] = {}

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

    @app_commands.command(name='match_create', description='Создать Bancho-лобби и начать pick/ban в игре')
    @app_commands.describe(player_one='osu!-ник первого игрока', player_two='osu!-ник второго ��грока', pool_name='Ranked-пул', format='Формат')
    @app_commands.choices(format=[app_commands.Choice(name='BO5 — 1 бан', value='5:1'), app_commands.Choice(name='BO7 — 1 бан', value='7:1'), app_commands.Choice(name='BO7 — 2 бана', value='7:2'), app_commands.Choice(name='BO9 — 2 бана', value='9:2')])
    @app_commands.autocomplete(pool_name=pool_name_autocomplete)
    async def match_create(self, interaction: discord.Interaction, player_one: str, player_two: str, pool_name: str, format: app_commands.Choice[str]):
        if not self.irc.configured:
            await interaction.response.send_message('❌ Добавь BANCHO_USERNAME и BANCHO_IRC_PASSWORD в приватный .env, затем перезапусти бота.', ephemeral=True); return
        if not player_one.strip() or _nick(player_one) == _nick(player_two):
            await interaction.response.send_message('❌ Укажи два разных osu!-ника.', ephemeral=True); return
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
            logger.info("Создание матча: %s vs %s, пул «%s», %s", player_one, player_two, pool['name'], format.name)
            channel = await self.irc.make_match(f'Ladder test — {player_one} vs {player_two}')
        except Exception as error:
            logger.exception("Не удалось создать Bancho-лобби")
            await interaction.followup.send(f'❌ Не удалось создать Bancho-лобби: {error}', ephemeral=True); return
        winner, loser = (player_one.strip(), player_two.strip()) if random.choice((True, False)) else (player_two.strip(), player_one.strip())
        map_choices = {
            item['slot'].upper(): {
                'beatmap_id': item['beatmap_id'],
                'mods': _multiplayer_mods(item['slot'], pool['mode']),
            }
            for item in pool['maps']
        }
        match_id = await create_match({'status':'waiting_players', 'discord_channel_id':interaction.channel_id, 'discord_message_id':None, 'bancho_channel':channel, 'bancho_match_id':channel.removeprefix('#mp_'), 'pool_id':pool['pool_id'], 'pool_name':pool['name'], 'mode':pool['mode'], 'players':[player_one.strip(), player_two.strip()], 'joined_players':[], 'roll_winner':winner, 'roll_loser':loser, 'best_of':best_of, 'bans_per_player':bans, 'actions':_actions(best_of,bans,loser,winner), 'action_index':0, 'available_slots':slots, 'tiebreaker_slot':tiebreaker, 'map_choices':map_choices, 'history':[]})
        logger.info("Матч #%s сохранён: %s, roll winner=%s", match_id, channel, winner)
        await self.irc.send_channel(channel, 'Waiting for both players to join before the roll.')
        match = await get_match(match_id)
        await interaction.followup.send(embed=self._embed(match))
        message = await interaction.original_response()
        await update_match(match_id, {'discord_message_id': message.id})
        self._invite_tasks[match_id] = asyncio.create_task(
            self._send_invites_for_five_minutes(match_id, channel, [player_one.strip(), player_two.strip()])
        )

    async def _send_invites_for_five_minutes(self, match_id: int, channel: str, players: list[str]) -> None:
        """Send five invite rounds, one per minute, while players are joining."""
        try:
            for attempt in range(1, 6):
                match = await get_match_by_bancho_channel(channel)
                if not match or match['status'] != 'waiting_players':
                    return
                joined = {_nick(name) for name in match.get('joined_players', [])}
                for player in players:
                    if _nick(player) not in joined:
                        await self.irc.send_channel(channel, f'!mp invite {player.replace(" ", "_")}')
                logger.info("Матч #%s: раунд инвайтов %s/5", match_id, attempt)
                if attempt < 5:
                    await asyncio.sleep(60)
            logger.info("Матч #%s: пятиминутное окно входа завершено", match_id)
            match = await get_match_by_bancho_channel(channel)
            if match and match['status'] == 'waiting_players':
                joined = match.get('joined_players', [])
                await update_match(match_id, {'status': 'cancelled'})
                logger.info("Матч #%s: отменён — в лобби вошли не все игроки (%s/2)", match_id, len(joined))
                await self.irc.send_channel(
                    channel,
                    f"Match cancelled: not all players joined within 5 minutes ({len(joined)}/2).",
                )
                await self.irc.send_channel(channel, '!mp close')
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

    async def _mark_player_joined(self, player: str, channel: str) -> None:
        match = await get_match_by_bancho_channel(channel)
        if not match or match['status'] != 'waiting_players':
            return
        expected = {_nick(name): name for name in match['players']}
        canonical = expected.get(_nick(player))
        if not canonical or canonical in match.get('joined_players', []):
            return
        joined = [*match.get('joined_players', []), canonical]
        logger.info("Матч #%s: %s вошёл в лобби (%s/2)", match['match_id'], canonical, len(joined))
        if len(joined) < len(match['players']):
            await update_match(match['match_id'], {'joined_players': joined})
            await self._refresh_discord(match['match_id'])
            return
        invite_task = self._invite_tasks.pop(match['match_id'], None)
        if invite_task and not invite_task.done():
            invite_task.cancel()
        await update_match(match['match_id'], {'joined_players': joined, 'status': 'pickban'})
        updated = await get_match(match['match_id'])
        await self.irc.send_channel(channel, f'Both players joined. Roll result: {updated["roll_winner"]} picks first; {updated["roll_loser"]} bans first.')
        await self.irc.send_channel(channel, f'{updated["roll_loser"]}: ban a slot by writing its name in this lobby chat.')
        await self.irc.send_channel(channel, f'!mp timer {self.ACTION_TIMER_SECONDS}')
        self._schedule_action_timeout(updated['match_id'])
        await self._refresh_discord(updated['match_id'])

    async def _refresh_discord(self, match_id: int) -> None:
        match = await get_match(match_id)
        if not match or not match.get('discord_message_id'):
            return
        try:
            channel = self.bot.get_channel(match['discord_channel_id'])
            if channel:
                message = await channel.fetch_message(match['discord_message_id'])
                await message.edit(embed=self._embed(match))
        except discord.DiscordException:
            logger.warning("Матч #%s: не удалось обновить Discord-сообщение", match_id)

    async def _set_map_and_wait_ready(self, match: dict, slot: str, *, is_tiebreaker: bool = False) -> None:
        """Apply one stored pool map, then wait for BanchoBot readiness."""
        choice = match['map_choices'][slot]
        mode_id = {'std': 0, 'taiko': 1, 'ctb': 2, 'mania': 3, 'mania4k': 3, 'mania7k': 3}[match['mode']]
        label = 'tiebreaker' if is_tiebreaker else 'picked map'
        logger.info("Матч #%s: установка %s %s (beatmap %s, mods %s)", match['match_id'], label, slot, choice['beatmap_id'], choice['mods'])
        await update_match(match['match_id'], {
            'status': 'waiting_ready', 'selected_slot': slot, 'selected_is_tiebreaker': is_tiebreaker,
            'current_map_scores': {},
        })
        await self.irc.send_channel(match['bancho_channel'], f"!mp map {choice['beatmap_id']} {mode_id}")
        await self.irc.send_channel(match['bancho_channel'], f"!mp mods {choice['mods']}")
        await self.irc.send_channel(match['bancho_channel'], 'Map is set. Waiting for: All players are ready.')
        await self.irc.send_channel(match['bancho_channel'], f'!mp timer {self.ACTION_TIMER_SECONDS}')

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
            errors: list[str] = []
            if settings.get('beatmap_id') != choice.get('beatmap_id'):
                errors.append(f"карта {settings.get('beatmap_id', '?')} вместо {choice.get('beatmap_id', '?')}")
            required = _mod_tokens(choice.get('mods', ''))
            active = _mod_tokens(settings.get('active_mods', ''))
            if 'freemod' in required:
                if 'freemod' not in active:
                    errors.append('в комнате не включён FreeMod')
                for player in settings.get('players', []):
                    if 'nf' not in _mod_tokens(','.join(player.get('mods', []))):
                        errors.append(f"у игрока {player.get('username', '?')} не включён NoFail")
            elif not required.issubset(active):
                errors.append(f"моды {settings.get('active_mods', 'None')} вместо {choice.get('mods', 'nf')}")
            if settings.get('player_count') != 2 or len(settings.get('players', [])) < 2:
                errors.append('в лобби не подтверждены оба игрока')
            elif any(player.get('status', '').casefold() != 'ready' for player in settings.get('players', [])):
                errors.append('не все игроки имеют статус Ready')
            if errors:
                logger.warning("Матч #%s: проверка !mp settings не пройдена: %s", match_id, '; '.join(errors))
                await update_match(match_id, {'status': 'waiting_ready'})
                await self.irc.send_channel(channel, 'Lobby check failed: ' + '; '.join(errors) + '. Map/mods will be reapplied.')
                await self._set_map_and_wait_ready(await get_match(match_id), selected)
            else:
                logger.info("Матч #%s: !mp settings подтверждены, запускаем карту %s", match_id, selected)
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
            self._settings_tasks.pop(match_id, None)

    def _schedule_action_timeout(self, match_id: int) -> None:
        old = self._action_timeout_tasks.pop(match_id, None)
        if old and not old.done():
            old.cancel()
        self._action_timeout_tasks[match_id] = asyncio.create_task(self._action_timeout(match_id))

    async def _action_timeout(self, match_id: int) -> None:
        try:
            await asyncio.sleep(self.ACTION_TIMER_SECONDS)
            match = await get_match(match_id)
            if not match or match['status'] != 'pickban':
                return
            action = match['actions'][match['action_index']]
            if action['kind'] == 'ban':
                await self.irc.send_channel(match['bancho_channel'], f"{action['player']} did not ban in time. Ban skipped.")
                await self._advance_action(match, None, automatic=True)
            else:
                if not match['available_slots']:
                    return
                slot = random.SystemRandom().choice(match['available_slots'])
                await self.irc.send_channel(match['bancho_channel'], f"{action['player']} did not pick in time. Pseudo-random pick: {slot}.")
                await self._advance_action(match, slot, automatic=True)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Матч #%s: ошибка обработки таймаута действия", match_id)
        finally:
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
            and text.strip().casefold() == 'all players are ready'
        ):
            selected = live_match.get('selected_slot', 'the selected map')
            logger.info("Матч #%s: все игроки готовы для %s; запрашиваем !mp settings", live_match['match_id'], selected)
            await update_match(live_match['match_id'], {'status': 'checking_settings'})
            old = self._settings_tasks.pop(live_match['match_id'], None)
            if old and not old.done(): old.cancel()
            self._settings_tasks[live_match['match_id']] = asyncio.create_task(
                self._check_settings_and_start(live_match['match_id'], channel)
            )
            await self._refresh_discord(live_match['match_id'])
            return
        if live_match and live_match['status'] == 'game_running' and sender.casefold() == 'banchobot':
            score_line = re.match(r"^(.+?) finished playing \(Score: ([\d,]+), (?:PASSED|FAILED)\)\.?$", text, re.I)
            if score_line:
                expected = {_nick(player): player for player in live_match['players']}
                player = expected.get(_nick(score_line.group(1)))
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
        if not match or _nick(sender) not in {_nick(p) for p in match['players']}: return
        action = match['actions'][match['action_index']]; slot = text.strip().upper()
        if _nick(sender) != _nick(action['player']) or slot not in match['available_slots']: return
        # Bancho's visible countdown is authoritative for the lobby.  Cancel
        # it in-game before handling a timely action; the next phase starts a
        # fresh `!mp timer 90` where appropriate.
        await self.irc.send_channel(channel, '!mp aborttimer')
        timeout_task = self._action_timeout_tasks.pop(match['match_id'], None)
        if timeout_task and not timeout_task.done():
            timeout_task.cancel()
        logger.info("Матч #%s: %s выбрал %s (%s)", match['match_id'], sender, slot, action['kind'])
        history = [*match['history'], {'kind':action['kind'], 'player':action['player'], 'slot':slot}]; next_index = match['action_index'] + 1
        # A picked map must be played before the next pick/ban turn. The final
        # regulation pick follows the same rule; TB remains reserved for the
        # deciding game and is not started at this stage.
        status = 'waiting_ready' if action['kind'] == 'pick' else 'pickban'
        await update_match(match['match_id'], {
            'history': history,
            'available_slots': [x for x in match['available_slots'] if x != slot],
            'action_index': next_index,
            'status': status,
            'selected_slot': slot if action['kind'] == 'pick' else None,
        })
        updated = await get_match(match['match_id'])
        if action['kind'] == 'pick':
            await self._set_map_and_wait_ready(updated, slot)
        else:
            turn = updated['actions'][next_index]
            await self.irc.send_channel(channel, f'{slot} {action["kind"]}ed by {action["player"]}. {turn["player"]}: {turn["kind"]} a slot.')
            await self.irc.send_channel(channel, f'!mp timer {self.ACTION_TIMER_SECONDS}')
            self._schedule_action_timeout(match['match_id'])
        try:
            discord_channel = self.bot.get_channel(updated['discord_channel_id'])
            if discord_channel and updated.get('discord_message_id'):
                message = await discord_channel.fetch_message(updated['discord_message_id'])
                await message.edit(embed=self._embed(updated))
        except discord.DiscordException:
            logger.warning("Матч #%s: не удалось обновить Discord-сообщение", match['match_id'])

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
        if scores[first] == scores[second]:
            await update_match(match['match_id'], {'status': 'pickban', 'current_map_scores': scores})
            await self.irc.send_channel(match['bancho_channel'], 'Map was tied. Referee review is required before continuing.')
            await self._refresh_discord(match['match_id'])
            return
        winner = first if scores[first] > scores[second] else second
        series = dict(match.get('series_score', {first: 0, second: 0}))
        series[winner] = int(series.get(winner, 0)) + 1
        played = [*match.get('played_maps', []), {
            'slot': match['selected_slot'], 'scores': scores, 'winner': winner,
            'is_tiebreaker': bool(match.get('selected_is_tiebreaker')),
        }]
        wins_needed = match['best_of'] // 2 + 1
        if series[winner] >= wins_needed:
            await update_match(match['match_id'], {'status': 'completed', 'series_score': series, 'played_maps': played})
            await self.irc.send_channel(match['bancho_channel'], f'{winner} wins the match {series[first]}-{series[second]}. GGWP!')
            self._schedule_room_close(match['match_id'], match['bancho_channel'])
        elif match.get('selected_is_tiebreaker'):
            await update_match(match['match_id'], {'status': 'completed', 'series_score': series, 'played_maps': played})
            await self.irc.send_channel(match['bancho_channel'], f'{winner} wins the tiebreaker {series[first]}-{series[second]}. GGWP!')
            self._schedule_room_close(match['match_id'], match['bancho_channel'])
        elif match['action_index'] >= len(match['actions']):
            if series[first] == series[second]:
                history = [*match['history'], {'kind': 'tiebreaker', 'player': 'automatic', 'slot': match['tiebreaker_slot']}]
                await update_match(match['match_id'], {'series_score': series, 'played_maps': played, 'history': history})
                updated = await get_match(match['match_id'])
                await self.irc.send_channel(match['bancho_channel'], f"Series is tied {series[first]}-{series[second]}. {updated['tiebreaker_slot']} is the tiebreaker.")
                await self._set_map_and_wait_ready(updated, updated['tiebreaker_slot'], is_tiebreaker=True)
            else:
                # This should only happen if no player reached the mathematical
                # match point due to an unusual external room intervention.
                await update_match(match['match_id'], {'status': 'completed', 'series_score': series, 'played_maps': played})
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
            logger.info("Матч #%s: отправлена команда !mp close", match_id)
        except asyncio.CancelledError:
            logger.info("Матч #%s: таймер закрытия отменён", match_id)
        except Exception:
            logger.exception("Матч #%s: не удалось закрыть MP-лобби", match_id)
        finally:
            self._close_tasks.pop(match_id, None)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OsuCommands(bot))
