from .base import ModeRules

CTB_RULES = ModeRules(
    mode="ctb", osu_ruleset="fruits",
    categories={"nm": "NoMod", "hd": "Hidden", "hr": "HardRock", "dt": "DoubleTime", "fm": "FreeMod", "tb": "Tiebreaker"},
    minimums={"nm": 3, "hd": 2, "hr": 2, "dt": 2, "tb": 1},
    optional_categories=("fm",),
    category_order=("nm", "hd", "hr", "dt", "fm", "tb"),
    slot_mods={"nm": (), "hd": ("HD",), "hr": ("FreeMod",), "dt": ("DT", "FreeMod"), "fm": ("FreeMod",), "tb": ("FreeMod",)},
    freemod_allowed_mods=("NoFail", "Hidden", "Hard Rock"),
    freemod_slot_allowed_mods={
        "hr": ("NoFail", "Hidden", "Hard Rock"),
        "dt": ("NoFail", "Hidden"),
    },
    freemod_slot_required_mods={"hr": ("Hard Rock",)},
    freemod_score_multipliers={
        "*": {
            "NoMod": 1.0,
            "Hidden": 1.0,
            "Hard Rock": 1.0,
        },
    },
    allow_std_converts=True,
)
