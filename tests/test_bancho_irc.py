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

    def test_parse_head_to_head_players_from_mp_log(self) -> None:
        settings = parse_match_settings(
            [
                "Beatmap: https://osu.ppy.sh/b/5398006 IOSYS - Endless Tewi-ma Park",
                "Team mode: HeadToHead, Win condition: ScoreV2",
                "Active mods: NoFail",
                "Players: 2",
                "Slot 1  Ready     https://osu.ppy.sh/u/35998757 f1ks14",
                "Slot 2  Ready     https://osu.ppy.sh/u/32246015 Nekish",
            ]
        )

        self.assertEqual(settings["player_count"], 2)
        self.assertEqual(
            settings["players"],
            [
                {
                    "slot": 1, "status": "Ready", "user_id": 35998757,
                    "username": "f1ks14", "team": None, "mods": [],
                },
                {
                    "slot": 2, "status": "Ready", "user_id": 32246015,
                    "username": "Nekish", "team": None, "mods": [],
                },
            ],
        )

    def test_parse_head_to_head_freemod_player_mods(self) -> None:
        settings = parse_match_settings(
            [
                "Team mode: HeadToHead, Win condition: ScoreV2",
                "Active mods: Freemod",
                "Players: 2",
                "Slot 1  Ready     https://osu.ppy.sh/u/35998757 f1ks14 [NoFail, Mirror]",
                "Slot 2  Ready     https://osu.ppy.sh/u/32246015 Nekish [No Fail, Fade In, Hidden, Flashlight]",
            ]
        )

        self.assertEqual(settings["players"][0]["mods"], ["nofail", "mirror"])
        self.assertEqual(
            settings["players"][1]["mods"],
            ["no fail", "fade in", "hidden", "flashlight"],
        )


if __name__ == "__main__":
    unittest.main()
