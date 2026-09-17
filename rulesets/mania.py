from .base import ModeRules

MANIA_4K_RULES = ModeRules(
    mode="mania4k", osu_ruleset="mania",
    categories={
        "rc": "Rices", "hb": "Hybrids", "ln": "Long Notes",
        "sv": "Speed Variations", "tb": "Tiebreaker",
    },
    minimums={"rc": 5, "hb": 2, "ln": 2, "tb": 1},
    optional_categories=("sv",),
    category_order=("rc", "hb", "ln", "sv", "tb"),
    slot_mods={"rc": ("FreeMod",), "hb": ("FreeMod",), "ln": ("FreeMod",), "sv": ("FreeMod",), "tb": ("FreeMod",)},
    allow_std_converts=True,
)

MANIA_7K_RULES = ModeRules(
    mode="mania7k", osu_ruleset="mania",
    categories={
        "rc": "Rices", "hb": "Hybrids", "ln": "Long Notes",
        "ex": "Extra", "tb": "Tiebreaker",
    },
    minimums={"rc": 5, "hb": 2, "ln": 2, "tb": 1},
    optional_categories=("ex",),
    category_order=("rc", "hb", "ln", "ex", "tb"),
    slot_mods={"rc": ("FreeMod",), "hb": ("FreeMod",), "ln": ("FreeMod",), "ex": ("FreeMod",), "tb": ("FreeMod",)},
    allow_std_converts=True,
)

# Backwards-compatible name for code that still imports MANIA_RULES.
MANIA_RULES = MANIA_4K_RULES
