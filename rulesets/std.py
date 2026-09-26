from .base import ModeRules

STD_RULES = ModeRules(
    mode="std", osu_ruleset="osu",
    categories={"nm": "NoMod", "hd": "Hidden", "hr": "HardRock", "dt": "DoubleTime", "fm": "FreeMod", "tb": "Tiebreaker"},
    minimums={"nm": 4, "hd": 2, "hr": 2, "dt": 2, "tb": 1},
    optional_categories=("fm",),
    category_order=("nm", "hd", "hr", "dt", "fm", "tb"),
    slot_mods={"nm": (), "hd": ("HD",), "hr": ("HR",), "dt": ("DT",), "fm": ("FreeMod",), "tb": ("FreeMod",)},
    freemod_allowed_mods=("NoFail", "Easy", "Hidden", "Hard Rock", "Flashlight"),
    freemod_slot_allowed_mods={},
    freemod_slot_required_mods={},
    freemod_score_multipliers={
        # All allowed STD FreeMod mods are explicit here. Easy is the only
        # manual ladder adjustment; osu! already applies HD/HR/FL effects.
        "fm": {
            "NoMod": 1.0,
            "Easy": 1.75,
            "Hidden": 1.0,
            "Hard Rock": 1.0,
            "Flashlight": 1.0,
        },
        "tb": {
            "NoMod": 1.0,
            "Easy": 1.75,
            "Hidden": 1.0,
            "Hard Rock": 1.0,
            "Flashlight": 1.0,
        },
    },
    allow_std_converts=False,
)
