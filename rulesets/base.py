"""Shared immutable model for a game mode's pool rules."""

from dataclasses import dataclass
from typing import Mapping, Tuple


@dataclass(frozen=True)
class ModeRules:
    mode: str
    osu_ruleset: str
    categories: Mapping[str, str]
    minimums: Mapping[str, int]
    optional_categories: Tuple[str, ...]
    category_order: Tuple[str, ...]
    slot_mods: Mapping[str, Tuple[str, ...]]
    allow_std_converts: bool

    @property
    def valid_categories(self) -> Tuple[str, ...]:
        return tuple(self.minimums) + self.optional_categories

    def category_from_slot(self, slot: str) -> str:
        normalized = slot.strip().upper()
        return "tb" if normalized == "TB" else "".join(filter(str.isalpha, normalized)).lower()

    def mods_for_slot(self, slot: str) -> Tuple[str, ...]:
        return self.slot_mods.get(self.category_from_slot(slot), ())
