from .base import ModeRules

STD_RULES = ModeRules(
    mode="std", osu_ruleset="osu",
    categories={"nm": "NoMod", "hd": "Hidden", "hr": "HardRock", "dt": "DoubleTime", "fm": "FreeMod", "tb": "Tiebreaker"},
    minimums={"nm": 4, "hd": 2, "hr": 2, "dt": 2, "tb": 1},
    optional_categories=("fm",),
    category_order=("nm", "hd", "hr", "dt", "fm", "tb"),
    slot_mods={"nm": (), "hd": ("HD",), "hr": ("HR",), "dt": ("DT",), "fm": (), "tb": ()},
    allow_std_converts=False,
)
