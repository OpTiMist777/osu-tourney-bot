from .base import ModeRules

TAIKO_RULES = ModeRules(
    mode="taiko", osu_ruleset="taiko",
    categories={"nm": "NoMod", "hd": "Hidden", "hr": "HardRock", "dt": "DoubleTime", "fm": "FreeMod", "tb": "Tiebreaker"},
    minimums={"nm": 3, "hd": 2, "hr": 2, "dt": 2, "fm": 1, "tb": 1},
    optional_categories=(),
    category_order=("nm", "hd", "hr", "dt", "fm", "tb"),
    slot_mods={"nm": (), "hd": ("HD",), "hr": ("HR",), "dt": ("DT",), "fm": (), "tb": ()},
    allow_std_converts=True,
)
