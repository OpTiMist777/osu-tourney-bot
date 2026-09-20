from .base import ModeRules

TAIKO_RULES = ModeRules(
    mode="taiko", osu_ruleset="taiko",
    categories={"nm": "NoMod", "hd": "Hidden", "hr": "HardRock", "dt": "DoubleTime", "fm": "FreeMod", "tb": "Tiebreaker"},
    minimums={"nm": 4, "hd": 2, "hr": 2, "dt": 2, "tb": 1},
    optional_categories=("fm",),
    category_order=("nm", "hd", "hr", "dt", "fm", "tb"),
    slot_mods={"nm": (), "hd": ("HD",), "hr": ("HR",), "dt": ("DT",), "fm": ("FreeMod",), "tb": ("FreeMod",)},
    freemod_allowed_mods=("NoFail", "Hidden", "Hard Rock"),
    freemod_slot_allowed_mods={},
    freemod_slot_required_mods={},
    freemod_score_multipliers={},
    allow_std_converts=True,
)
