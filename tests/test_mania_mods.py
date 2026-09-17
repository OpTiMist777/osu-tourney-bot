import unittest

from cogs.osu_commands import (
    MANIA_ALLOWED_PLAYER_MODS,
    _mod_tokens,
    _multiplayer_mods,
)


class TestManiaMatchMods(unittest.TestCase):
    def test_every_mania_category_uses_freemod_with_nofail(self) -> None:
        for mode, slots in {
            "mania4k": ("RC1", "HB1", "LN1", "SV1", "TB"),
            "mania7k": ("RC1", "HB1", "LN1", "EX1", "TB"),
        }.items():
            for slot in slots:
                self.assertEqual(_multiplayer_mods(slot, mode), "nf freemod")

    def test_only_configured_mania_player_mods_are_allowed(self) -> None:
        self.assertEqual(
            _mod_tokens("No Fail, Mirror, Fade In, Hidden, Flashlight"),
            MANIA_ALLOWED_PLAYER_MODS,
        )
        self.assertEqual(_mod_tokens("NoFail, Fade-in"), {"nf", "fi"})
        self.assertEqual(_mod_tokens("NoFail, DoubleTime") - MANIA_ALLOWED_PLAYER_MODS, {"dt"})


if __name__ == "__main__":
    unittest.main()
