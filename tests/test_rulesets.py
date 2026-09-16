import unittest

from rulesets import MANIA_4K_RULES, MANIA_RULES, get_ruleset, normalize_mode
from utils import (
    format_category_requirements,
    format_maps_by_category,
    parse_category_maps,
    validate_pool_maps,
)


class TestRulesets(unittest.TestCase):
    def test_mode_aliases(self) -> None:
        self.assertEqual(normalize_mode("osu"), "std")
        self.assertEqual(normalize_mode("catch"), "ctb")
        self.assertIs(get_ruleset("mania"), MANIA_4K_RULES)
        self.assertIs(MANIA_RULES, MANIA_4K_RULES)

    def test_parse_category_maps(self) -> None:
        maps, error = parse_category_maps("nm:123,456 hd:789 tb:999", "std")
        self.assertEqual(error, "")
        self.assertEqual(maps, [("NM1", 123), ("NM2", 456), ("HD1", 789), ("TB", 999)])

    def test_validate_pool_maps(self) -> None:
        maps = [
            ("NM1", 1),
            ("NM2", 2),
            ("NM3", 3),
            ("NM4", 4),
            ("HD1", 5),
            ("HD2", 6),
            ("HR1", 7),
            ("HR2", 8),
            ("DT1", 9),
            ("DT2", 10),
            ("TB", 11),
        ]
        valid, message = validate_pool_maps(maps, "std")
        self.assertTrue(valid)
        self.assertIn("соответствует", message)

        invalid, message = validate_pool_maps(maps + [("NM1", 12)], "std")
        self.assertFalse(invalid)
        self.assertIn("дублирующиеся слоты", message)

    def test_formatters(self) -> None:
        requirements = format_category_requirements("std")
        self.assertIn("Обязательные категории", requirements)
        self.assertIn("Опциональные категории", requirements)

        formatted = format_maps_by_category(
            [
                {"slot": "TB", "beatmap_id": 99},
                {"slot": "HD1", "beatmap_id": 2},
                {"slot": "NM2", "beatmap_id": 1},
                {"slot": "NM1", "beatmap_id": 3},
            ],
            "std",
        )
        self.assertIn("**NoMod**", formatted)
        self.assertIn("[NM1](https://osu.ppy.sh/b/3)", formatted)
        self.assertLess(formatted.index("**NoMod**"), formatted.index("**Hidden**"))
        self.assertLess(formatted.index("**Hidden**"), formatted.index("**Tiebreaker**"))


if __name__ == "__main__":
    unittest.main()
