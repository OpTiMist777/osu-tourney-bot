# utils.py
from typing import Dict, List, Tuple
import re
from rulesets import RULESETS, get_ruleset, normalize_mode

CATEGORY_FULL_NAMES = {category: name for ruleset in RULESETS.values() for category, name in ruleset.categories.items()}

def is_valid_mode(mode: str) -> bool:
    return normalize_mode(mode) in RULESETS

def get_minimum_slots(mode: str) -> Dict[str, int]:
    norm_mode = normalize_mode(mode)
    return dict(get_ruleset(norm_mode).minimums)

def get_valid_categories(mode: str) -> List[str]:
    norm_mode = normalize_mode(mode)
    return list(get_ruleset(norm_mode).valid_categories)

def parse_category_maps(category_maps_text: str, mode: str) -> Tuple[List[Tuple[str, int]], str]:
    norm_mode = normalize_mode(mode)
    valid_categories = get_valid_categories(norm_mode)
    maps: List[Tuple[str, int]] = []
    errors = []
    
    parts = re.split(r'\s+', category_maps_text.strip())
    
    for part in parts:
        if not part or ':' not in part:
            continue
        
        try:
            cat_part, ids_part = part.split(':', 1)
            category = cat_part.strip().lower()
            ids_str = ids_part.strip()
            
            if category not in valid_categories and category != 'tb':
                valid_str = ", ".join([f"`{c}`" for c in sorted(valid_categories + ['tb'])])
                errors.append(f"❌ Недопустимая категория `{category}` для режима {norm_mode.upper()}.\nДопустимые: {valid_str}")
                continue
            
            id_list = [id_str.strip() for id_str in ids_str.split(',') if id_str.strip()]
            
            if not id_list:
                errors.append(f"❌ Категория `{category}` не содержит ID карт")
                continue
            
            for idx, id_str in enumerate(id_list, 1):
                try:
                    beatmap_id = int(id_str)
                    if beatmap_id <= 0:
                        raise ValueError("ID должен быть положительным")
                    
                    slot = "TB" if category == "tb" else f"{category.upper()}{idx}"
                    maps.append((slot, beatmap_id))
                    
                except ValueError:
                    errors.append(f"❌ Неверный формат ID в `{category}`: `{id_str}` (должно быть число)")
        
        except Exception as e:
            errors.append(f"❌ Ошибка обработки части `{part}`: {str(e)}")
    
    if errors:
        return [], "\n".join(errors)
    
    return maps, ""


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
                errors.append(f"❌ Недопустимая категория `{raw_category}` для {norm_mode.upper()}. Допустимые: {choices}")
                category = None
                continue
            token = first_id

        if category is None:
            errors.append(f"❌ ID `{token}` указан до категории. Используйте формат `NM:123 321 HD:456`.")
            continue
        if not token:
            errors.append(f"❌ После `{category.upper()}:` должен быть ID карты.")
            continue
        try:
            beatmap_id = int(token)
            if beatmap_id <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"❌ Неверный ID карты `{token}`.")
            continue
        category_ids.setdefault(category, []).append(beatmap_id)

    for category, ids in category_ids.items():
        for index, beatmap_id in enumerate(ids, 1):
            maps.append(("TB" if category == "tb" else f"{category.upper()}{index}", beatmap_id))

    if errors:
        return [], '\n'.join(errors)
    if not maps:
        return [], "❌ Не найдено ни одной карты. Используйте формат `NM:123 321 HD:456`."
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
            missing.append(f"• **{cat_name}**: требуется минимум {min_count}, указано {actual}")
    
    if missing:
        return False, (
            "❌ **Не выполнены минимальные требования:**\n" +
            "\n".join(missing) +
            f"\n\n💡 Совет: Добавьте недостающие карты. "
            f"Пример для STD: `nm:123,456,789,012 hd:345,678 hr:901,234 dt:567,890 tb:111`"
        )
    
    slots = [slot for slot, _ in maps]
    if len(slots) != len(set(slots)):
        duplicates = set([s for s in slots if slots.count(s) > 1])
        return False, f"❌ Обнаружены дублирующиеся слоты: {', '.join(duplicates)}"
    
    return True, "✅ Пул соответствует минимальным требованиям"

def format_category_requirements(mode: str) -> str:
    norm_mode = normalize_mode(mode)
    min_req = get_minimum_slots(norm_mode)
    optional = get_ruleset(norm_mode).optional_categories
    
    lines = ["**Обязательные категории:**"]
    for cat in get_ruleset(norm_mode).category_order:
        if cat not in min_req:
            continue
        min_count = min_req[cat]
        name = CATEGORY_FULL_NAMES.get(cat, cat.upper())
        lines.append(f"• `{cat.upper()}` — {name} (минимум {min_count} карт)")
    
    if optional:
        lines.append("\n**Опциональные категории:**")
        for cat in get_ruleset(norm_mode).category_order:
            if cat not in optional:
                continue
            name = CATEGORY_FULL_NAMES.get(cat, cat.upper())
            lines.append(f"• `{cat.upper()}` — {name} (0+ карт)")
    
    lines.append("\n💡 Можно добавлять **больше карт**, чем минимальный набор!")
    return "\n".join(lines)

def format_maps_by_category(maps: List[Dict], mode: str) -> str:
    """
    Группирует карты по категориям в строго правильном порядке для режима
    """
    if not maps:
        return "📭 Нет карт"
    
    # Правильный порядок категорий для каждого режима
    ruleset = get_ruleset(mode)
    order = ruleset.category_order
    
    # Группируем карты по категориям
    categories: Dict[str, List[Tuple[str, int]]] = {}
    for map_dict in maps:
        slot = map_dict['slot']
        bm_id = map_dict['beatmap_id']
        
        # Извлекаем категорию из слота (nm1 → nm, tb → tb)
        cat = ruleset.category_from_slot(slot)
        
        # СОХРАНЯЕМ КАТЕГОРИЮ В НЕИЗМЕНЕННОМ ВИДЕ
        categories.setdefault(cat, []).append((slot, bm_id))
    
    # СОЗДАЕМ СПИСОК КАТЕГОРИЙ В ПРАВИЛЬНОМ ПОРЯДКЕ
    sorted_categories = []
    
    # Сначала добавляем категории из заданного порядка
    for cat in order:
        if cat in categories:
            sorted_categories.append(cat)
    
    # Затем добавляем остальные категории (для непредвиденных случаев)
    for cat in categories:
        if cat not in sorted_categories:
            sorted_categories.append(cat)
    
    # Формируем строки с картами
    lines = []
    for cat in sorted_categories:
        # Используем оригинальное название категории из пула
        name = CATEGORY_FULL_NAMES.get(cat, cat.upper())
        cat_maps = categories[cat]
        
        # Сортируем карты внутри категории по номеру (nm1, nm2, nm3)
        cat_maps_sorted = sorted(
            cat_maps, 
            key=lambda x: (x[0][0].lower(), int(''.join(filter(str.isdigit, x[0])) if x[0][1:].isdigit() else 0))
        )
        
        # Формируем список карт
        links = [f"[{slot}](https://osu.ppy.sh/b/{bm_id})" for slot, bm_id in cat_maps_sorted]
        lines.append(f"**{name}** ({len(cat_maps)}): {', '.join(links)}")
    
    return "\n".join(lines)

def format_pool_cards(cards_info: list, max_per_field: int = 10) -> list[tuple[str, str]]:
    """
    Разбивает список карт на части для embed полей (макс. 1024 символа на поле)
    Возвращает список кортежей (имя_поля, значение_поля)
    """
    fields = []
    current_chunk = []
    current_length = 0
    
    for item in cards_info:
        item_length = len(item) + 2  # + "\n"
        
        if current_length + item_length > 1000 or len(current_chunk) >= max_per_field:
            if current_chunk:
                fields.append(("\n".join(current_chunk), len(current_chunk)))
                current_chunk = []
                current_length = 0
        
        current_chunk.append(item)
        current_length += item_length
    
    if current_chunk:
        fields.append(("\n".join(current_chunk), len(current_chunk)))
    
    result = []
    total_fields = len(fields)
    for i, (content, count) in enumerate(fields, 1):
        name = f"Карты ({count})" if total_fields == 1 else f"Карты {i}/{total_fields} ({count})"
        result.append((name, content))
    
    return result
