import unittest

from cogs.osu_commands import (
    SLOT_INPUT_PATTERN,
    _freemod_allowed_tokens,
    _freemod_instruction,
    _freemod_required_tokens,
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
            _freemod_allowed_tokens("mania4k"),
        )
        self.assertEqual(_mod_tokens("NoFail, Fade-in"), {"nf", "fi"})
        self.assertEqual(_mod_tokens("NoFail, DoubleTime") - _freemod_allowed_tokens("mania4k"), {"dt"})

    def test_taiko_freemod_allows_only_nofail_hidden_and_hardrock(self) -> None:
        self.assertEqual(_freemod_allowed_tokens("taiko"), {"nf", "hd", "hr"})
        self.assertEqual(_freemod_allowed_tokens("taiko") - _mod_tokens("NoFail, Hidden, HardRock"), set())
        self.assertEqual(_mod_tokens("NoFail, DoubleTime") - _freemod_allowed_tokens("taiko"), {"dt"})
        self.assertEqual(
            _freemod_instruction("taiko"),
            "Taiko FreeMod: enable NoFail before Ready. Optional mods: Hidden, Hard Rock.",
        )

    def test_ctb_freemod_allows_only_nofail_hidden_and_hardrock(self) -> None:
        self.assertEqual(_freemod_allowed_tokens("ctb"), {"nf", "hd", "hr"})
        self.assertEqual(
            _freemod_instruction("ctb"),
            "CTB FreeMod: enable NoFail before Ready. Optional mods: Hidden, Hard Rock.",
        )

    def test_ctb_hardrock_and_doubletime_slots_are_hybrid_freemod(self) -> None:
        self.assertEqual(_multiplayer_mods("HR1", "ctb"), "nf freemod")
        self.assertEqual(_multiplayer_mods("DT1", "ctb"), "nf dt freemod")
        self.assertEqual(_freemod_allowed_tokens("ctb", "HR1"), {"nf", "hd", "hr"})
        self.assertEqual(_freemod_allowed_tokens("ctb", "DT1"), {"nf", "hd"})
        self.assertEqual(_freemod_required_tokens("ctb", "HR1"), {"hr"})
        self.assertEqual(_freemod_required_tokens("ctb", "DT1"), set())
        self.assertEqual(
            _freemod_instruction("ctb", "DT1"),
            "CTB FreeMod: enable NoFail before Ready. Optional mods: Hidden.",
        )

    def test_slot_input_pattern_rejects_chat_but_accepts_slot_codes(self) -> None:
        self.assertIsNotNone(SLOT_INPUT_PATTERN.fullmatch("RC2"))
        self.assertIsNotNone(SLOT_INPUT_PATTERN.fullmatch("TB"))
        self.assertIsNone(SLOT_INPUT_PATTERN.fullmatch("okay"))


if __name__ == "__main__":
    unittest.main()
