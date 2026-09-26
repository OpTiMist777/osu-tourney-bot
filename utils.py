# utils.py
from typing import Dict, List, Tuple
import re
from rulesets import RULESETS, get_ruleset, normalize_mode

CATEGORY_FULL_NAMES = {category: name for ruleset in RULESETS.values() for category, name in ruleset.categories.items()}

def get_minimum_slots(mode: str) -> Dict[str, int]:
    norm_mode = normalize_mode(mode)
    return dict(get_ruleset(norm_mode).minimums)

def get_valid_categories(mode: str) -> List[str]:
    norm_mode = normalize_mode(mode)
    return list(get_ruleset(norm_mode).valid_categories)

def parse_spaced_category_maps(category_maps_text: str, mode: str) -> Tuple[List[Tuple[str, int]], str]:
    """Parse slash input such as ``NM:123 321 HD:456 789 TB:999``."""
    norm_mode = normalize_mode(mode)
    valid_categories = set(get_valid_categories(norm_mode))
    maps: List[Tuple[str, int]] = []
    errors = []
    category = None
    category_ids: Dict[str, List[int]] = {}

    for token in category_maps_text.split():
        if ':' in token:
            raw_category, first_id = token.split(':', 1)
            category = raw_category.lower().strip()
            if category not in valid_categories:
                choices = ', '.join(f'`{item.upper()}`' for item in get_valid_categories(norm_mode))
                errors.append(f"❌ Invalid category `{raw_category}` for {norm_mode.upper()}. Allowed: {choices}")
                category = None
                continue
            token = first_id

        if category is None:
            errors.append(f"❌ ID `{token}` appears before a category. Use `NM:123 321 HD:456` format.")
            continue
        if not token:
            errors.append(f"❌ `{category.upper()}:` must be followed by a beatmap ID.")
            continue
        try:
            beatmap_id = int(token)
            if beatmap_id <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"❌ Invalid beatmap ID `{token}`.")
            continue
        category_ids.setdefault(category, []).append(beatmap_id)

    for category, ids in category_ids.items():
        for index, beatmap_id in enumerate(ids, 1):
            maps.append(("TB" if category == "tb" else f"{category.upper()}{index}", beatmap_id))

    if errors:
        return [], '\n'.join(errors)
    if not maps:
        return [], "❌ No maps found. Use `NM:123 321 HD:456` format."
    return maps, ""

def validate_pool_maps(maps: List[Tuple[str, int]], mode: str) -> Tuple[bool, str]:
    norm_mode = normalize_mode(mode)
    min_requirements = get_minimum_slots(norm_mode)
    
    category_counts: Dict[str, int] = {}
    for slot, _ in maps:
        cat = get_ruleset(norm_mode).category_from_slot(slot)
        category_counts[cat] = category_counts.get(cat, 0) + 1
    
    missing = []
    for cat, min_count in min_requirements.items():
        actual = category_counts.get(cat, 0)
        if actual < min_count:
            cat_name = CATEGORY_FULL_NAMES.get(cat, cat.upper())
            missing.append(f"• **{cat_name}**: minimum {min_count} required, {actual} provided")
    
    if missing:
        return False, (
            "❌ **Minimum requirements are not met:**\n" +
            "\n".join(missing) +
            f"\n\n💡 Tip: Add the missing maps. "
            f"STD example: `nm:123,456,789,012 hd:345,678 hr:901,234 dt:567,890 tb:111`"
        )
    
    slots = [slot for slot, _ in maps]
    if len(slots) != len(set(slots)):
        duplicates = set([s for s in slots if slots.count(s) > 1])
        return False, f"❌ Duplicate slots found: {', '.join(duplicates)}"
    
    return True, "✅ Pool meets the minimum requirements"

def format_category_requirements(mode: str) -> str:
    norm_mode = normalize_mode(mode)
    min_req = get_minimum_slots(norm_mode)
    optional = get_ruleset(norm_mode).optional_categories
    
    lines = ["**Required categories:**"]
    for cat in get_ruleset(norm_mode).category_order:
        if cat not in min_req:
            continue
        min_count = min_req[cat]
        name = CATEGORY_FULL_NAMES.get(cat, cat.upper())
        lines.append(f"• `{cat.upper()}` — {name} (minimum {min_count} maps)")
    
    if optional:
        lines.append("\n**Optional categories:**")
        for cat in get_ruleset(norm_mode).category_order:
            if cat not in optional:
                continue
            name = CATEGORY_FULL_NAMES.get(cat, cat.upper())
            lines.append(f"• `{cat.upper()}` — {name} (0+ maps)")
    
    lines.append("\n💡 You can add **more maps** than the minimum set!")
    return "\n".join(lines)


def format_maps_by_category(maps: List[Dict], mode: str) -> str:
    """
    Group maps by category in the configured mode order.
    """
    if not maps:
        return "📭 No maps"
    
    # Configured category order for each mode
    ruleset = get_ruleset(mode)
    order = ruleset.category_order
    
    # Group maps by category
    categories: Dict[str, List[Tuple[str, int]]] = {}
    for map_dict in maps:
        slot = map_dict['slot']
        bm_id = map_dict['beatmap_id']
        
        # Extract the category from the slot (nm1 → nm, tb → tb)
        cat = ruleset.category_from_slot(slot)
        
        # Preserve the category exactly as stored in the pool
        categories.setdefault(cat, []).append((slot, bm_id))
    
    # Build categories in the configured order
    sorted_categories = []
    
    # Add configured categories first
    for cat in order:
        if cat in categories:
            sorted_categories.append(cat)
    
    # Then append unexpected categories
    for cat in categories:
        if cat not in sorted_categories:
            sorted_categories.append(cat)
    
    # Build map lines
    lines = []
    for cat in sorted_categories:
        # Use the original category name from the pool
        name = CATEGORY_FULL_NAMES.get(cat, cat.upper())
        cat_maps = categories[cat]
        
        # Sort maps inside a category by slot number
        cat_maps_sorted = sorted(
            cat_maps, 
            key=lambda x: (x[0][0].lower(), int(''.join(filter(str.isdigit, x[0])) if x[0][1:].isdigit() else 0))
        )
        
        # Build the formatted map list
        links = [f"[{slot}](https://osu.ppy.sh/b/{bm_id})" for slot, bm_id in cat_maps_sorted]
        lines.append(f"**{name}** ({len(cat_maps)}): {', '.join(links)}")
    
    return "\n".join(lines)
