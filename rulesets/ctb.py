from .base import ModeRules

CTB_RULES = ModeRules(
    mode="ctb", osu_ruleset="fruits",
    categories={"nm": "NoMod", "hd": "Hidden", "hr": "HardRock", "dt": "DoubleTime", "fm": "FreeMod", "tb": "Tiebreaker"},
    minimums={"nm": 3, "hd": 2, "hr": 2, "dt": 3, "tb": 1},
    optional_categories=("fm",),
    category_order=("nm", "hd", "hr", "dt", "fm", "tb"),
    slot_mods={"nm": (), "hd": ("HD",), "hr": ("HR",), "dt": ("DT",), "fm": (), "tb": ()},
    allow_std_converts=True,
)
