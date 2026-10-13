"""Fixed user-game categories, separate from engines and content tags."""

CATEGORIES = (
    ("galgame", "Galgame"),
    ("slg", "SLG"),
    ("rpg", "RPG"),
    ("act", "ACT"),
    ("simulation", "模拟经营"),
    ("casual_puzzle", "休闲/解谜"),
    ("other", "其他"),
)
CATEGORY_LABELS = dict(CATEGORIES)


def normalize_categories(category_ids):
    """Validate IDs and return a deduplicated tuple in the fixed UI order."""
    if isinstance(category_ids, (str, bytes)) or category_ids is None:
        raise ValueError("游戏分类必须是分类 ID 集合。")
    try:
        values = tuple(category_ids)
    except TypeError as exc:
        raise ValueError("游戏分类必须是分类 ID 集合。") from exc
    if any(not isinstance(value, str) or value not in CATEGORY_LABELS
           for value in values):
        raise ValueError("包含无法识别的游戏分类。")
    selected = set(values)
    return tuple(category_id for category_id, _ in CATEGORIES
                 if category_id in selected)


def format_categories(category_ids):
    """Display the selected categories, or the explicit unclassified label."""
    return " · ".join(CATEGORY_LABELS[category_id]
                      for category_id in normalize_categories(category_ids)) or "未分类"
