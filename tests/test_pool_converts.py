import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.pool_commands import PoolCommands
from osu_api import osu_manager


class TestPoolConverts(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cog = PoolCommands(MagicMock())
        self.cog.PARSE_DELAY_SECONDS = 0
        self.std_beatmap = {
            "id": 3865326,
            "set_id": 123,
            "title": "Convert test",
            "artist": "Test artist",
            "difficulty": "Insane",
            "stars": 5.0,
            "mode": "osu",
            "bpm": 180.0,
            "length": 120,
            "cs": 4.0,
            "ar": 9.0,
            "od": 8.0,
            "convert": False,
            "url": "https://osu.ppy.sh/b/3865326",
        }

    async def test_std_source_is_accepted_as_ctb_convert(self) -> None:
        with (
            patch("builtins.print"),
            patch.object(osu_manager, "get_beatmap", AsyncMock(return_value=self.std_beatmap)),
            patch.object(osu_manager, "get_beatmap_star_rating", AsyncMock(return_value=3.21)) as get_sr,
        ):
            snapshot = await self.cog._parse_map_snapshot("NM1", 3865326, "ctb")

        self.assertTrue(snapshot["is_convert"])
        self.assertEqual(snapshot["source_mode"], "osu")
        self.assertEqual(snapshot["target_mode"], "ctb")
        self.assertEqual(snapshot["star_rating"], 3.21)
        get_sr.assert_awaited_once_with(3865326, [], ruleset="fruits")

    async def test_other_nonstd_source_is_not_a_ctb_convert(self) -> None:
        incompatible = {**self.std_beatmap, "mode": "mania"}
        with (
            patch("builtins.print"),
            patch.object(osu_manager, "get_beatmap", AsyncMock(return_value=incompatible)),
        ):
            with self.assertRaises(ValueError):
                await self.cog._parse_map_snapshot("NM1", 3865326, "ctb")


if __name__ == "__main__":
    unittest.main()
