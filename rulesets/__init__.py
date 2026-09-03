"""Mode-specific ladder and pool rules."""

from .base import ModeRules
from .ctb import CTB_RULES
from .mania import MANIA_4K_RULES, MANIA_7K_RULES, MANIA_RULES
from .std import STD_RULES
from .taiko import TAIKO_RULES

RULESETS = {
    "std": STD_RULES,
    "taiko": TAIKO_RULES,
    "ctb": CTB_RULES,
    "mania": MANIA_4K_RULES,
    "mania4k": MANIA_4K_RULES,
    "mania7k": MANIA_7K_RULES,
}

MODE_ALIASES = {
    "std": "std", "osu": "std", "standard": "std",
    "taiko": "taiko", "tk": "taiko",
    "ctb": "ctb", "catch": "ctb", "fruits": "ctb",
    "mania": "mania4k", "man": "mania4k", "mania4k": "mania4k", "4k": "mania4k",
    "mania7k": "mania7k", "7k": "mania7k",
}


def normalize_mode(mode: str) -> str:
    return MODE_ALIASES.get(mode.lower().strip(), mode.lower().strip())


def get_ruleset(mode: str) -> ModeRules:
    normalized = normalize_mode(mode)
    try:
        return RULESETS[normalized]
    except KeyError as error:
        raise ValueError(f"Unsupported mode: {mode}") from error
