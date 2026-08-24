from .base import ModeRules

MANIA_RULES = ModeRules(
    mode="mania", osu_ruleset="mania",
    categories={
        "rc": "Rices", "hb": "Hybrids", "ln": "Long Notes",
        "sv": "Speed Variations", "ex": "Extra", "tb": "Tiebreaker",
    },
    # Mania is the one exception: 9 regulation maps plus one tiebreaker.
    minimums={"rc": 5, "hb": 2, "ln": 2, "tb": 1},
    optional_categories=("sv", "ex"),
    category_order=("rc", "hb", "ln", "sv", "ex", "tb"),
    slot_mods={"rc": (), "hb": (), "ln": (), "sv": (), "ex": (), "tb": ()},
    allow_std_converts=True,
)
