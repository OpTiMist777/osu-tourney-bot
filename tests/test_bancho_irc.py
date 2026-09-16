import unittest

from bancho_irc import parse_match_settings


class TestBanchoSettings(unittest.TestCase):
    def test_parse_settings_snapshot_from_mp_log(self) -> None:
        settings = parse_match_settings(
            [
                "Room name: Ladder test, History: https://osu.ppy.sh/mp/121720831",
                "Beatmap: https://osu.ppy.sh/b/5636267 Yousei Teikoku - Zetsubou plantation",
                "Team mode: TeamVs, Win condition: ScoreV2",
                "Active mods: Freemod",
                "Players: 2",
                "Slot 1  Ready     https://osu.ppy.sh/u/12139655 [Slick]         [Team Red  / NoFail, Hidden]",
                "Slot 2  Ready     https://osu.ppy.sh/u/10646481 TheEpicFilipino [Team Blue / NoFail, Hidden]",
            ]
        )

        self.assertEqual(settings["beatmap_id"], 5636267)
        self.assertEqual(settings["active_mods"], "freemod")
        self.assertEqual(settings["player_count"], 2)
        self.assertEqual(len(settings["players"]), 2)
        self.assertEqual(settings["players"][0]["username"], "[Slick]")
        self.assertEqual(settings["players"][0]["mods"], ["nofail", "hidden"])
        self.assertTrue(all(player["status"] == "Ready" for player in settings["players"]))


if __name__ == "__main__":
    unittest.main()
