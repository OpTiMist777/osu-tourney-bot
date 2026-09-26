import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.osu_commands import OsuCommands


class TestReadyTimeout(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cog = OsuCommands(MagicMock())
        self.cog.irc = MagicMock()
        self.cog.irc.send_channel = AsyncMock()
        self.cog.irc.get_match_settings = AsyncMock()
        self.cog._post_match_log = AsyncMock()
        self.cog._refresh_discord = AsyncMock()

    async def test_expired_ready_timer_requests_forced_settings_check(self) -> None:
        self.cog.ACTION_TIMER_SECONDS = 0
        self.cog._queue_settings_check = AsyncMock()
        match = {"match_id": 7, "status": "waiting_ready"}

        with patch("cogs.osu_commands.get_match", AsyncMock(return_value=match)):
            await self.cog._ready_timeout(7, "#mp_7")

        self.cog._queue_settings_check.assert_awaited_once_with(7, "#mp_7", force_start=True)
        self.cog.irc.send_channel.assert_awaited_once_with(
            "#mp_7", "Ready timer expired. Verifying map and mods before forced start."
        )

    async def test_forced_start_allows_not_ready_after_lobby_validation(self) -> None:
        match = {
            "match_id": 7,
            "status": "checking_settings",
            "mode": "mania4k",
            "selected_slot": "RC1",
            "map_choices": {"RC1": {"beatmap_id": 123, "mods": "nf freemod"}},
            "player_osu_ids": [1, 2],
        }
        self.cog.irc.get_match_settings.return_value = {
            "beatmap_id": 123,
            "active_mods": "freemod",
            "player_count": 2,
            "players": [
                {"user_id": 1, "username": "one", "status": "Not Ready", "mods": ["NoFail"]},
                {"user_id": 2, "username": "two", "status": "Not Ready", "mods": ["NoFail", "FadeIn"]},
            ],
        }

        with (
            patch("cogs.osu_commands.asyncio.sleep", AsyncMock()),
            patch("cogs.osu_commands.get_match", AsyncMock(return_value=match)),
            patch("cogs.osu_commands.update_match", AsyncMock()) as update_match,
        ):
            await self.cog._check_settings_and_start(7, "#mp_7", force_start=True)

        update_match.assert_awaited_once_with(
            7,
            {"status": "game_running", "current_map_player_mods": {}},
        )
        self.cog.irc.send_channel.assert_has_awaits([
            unittest.mock.call("#mp_7", "!mp aborttimer"),
            unittest.mock.call("#mp_7", "!mp start 5"),
        ])

    async def test_taiko_freemod_rejects_disallowed_personal_mod(self) -> None:
        match = {
            "match_id": 7,
            "status": "checking_settings",
            "mode": "taiko",
            "selected_slot": "FM1",
            "map_choices": {"FM1": {"beatmap_id": 123, "mods": "nf freemod"}},
            "player_osu_ids": [1, 2],
        }
        self.cog.irc.get_match_settings.return_value = {
            "beatmap_id": 123,
            "active_mods": "freemod",
            "player_count": 2,
            "players": [
                {"user_id": 1, "username": "one", "status": "Ready", "mods": ["NoFail", "Hidden"]},
                {"user_id": 2, "username": "two", "status": "Ready", "mods": ["NoFail", "DoubleTime"]},
            ],
        }
        self.cog._set_map_and_wait_ready = AsyncMock()

        with (
            patch("cogs.osu_commands.asyncio.sleep", AsyncMock()),
            patch("cogs.osu_commands.get_match", AsyncMock(return_value=match)),
            patch("cogs.osu_commands.update_match", AsyncMock()),
        ):
            await self.cog._check_settings_and_start(7, "#mp_7")

        self.cog._set_map_and_wait_ready.assert_awaited_once_with(match, "FM1")
        self.cog.irc.send_channel.assert_awaited_once()
        self.assertIn("disallowed mods: dt", self.cog.irc.send_channel.await_args.args[1])

    async def test_std_easy_score_is_adjusted_by_one_point_seven_five(self) -> None:
        match = {
            "match_id": 7,
            "mode": "std",
            "status": "game_running",
            "selected_slot": "FM1",
            "selected_is_tiebreaker": False,
            "map_choices": {"FM1": {"mods": "nf freemod"}},
            "current_map_scores": {"one": 100, "two": 150},
            "current_map_player_mods": {"one": ["ez", "nf"], "two": ["nf", "hd"]},
            "players": ["one", "two"],
            "series_score": {"one": 0, "two": 0},
            "played_maps": [],
            "best_of": 5,
            "action_index": 0,
            "actions": [{"player": "two", "kind": "pick"}],
            "bancho_channel": "#mp_7",
        }

        with (
            patch("cogs.osu_commands.get_match", AsyncMock(return_value=match)),
            patch("cogs.osu_commands.update_match", AsyncMock()) as update_match,
        ):
            await self.cog._finish_played_map(match)

        update = update_match.await_args_list[-1].args[1]
        played = update["played_maps"][0]
        self.assertEqual(played["raw_scores"], {"one": 100, "two": 150})
        self.assertEqual(played["scores"], {"one": 175, "two": 150})
        self.assertEqual(played["score_multipliers"], {"one": 1.75, "two": 1.0})
        self.assertEqual(played["winner"], "one")


if __name__ == "__main__":
    unittest.main()
