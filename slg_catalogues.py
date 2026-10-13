"""Physical catalogue identities and the virtual union shown by Windows."""

CATALOGUES = {
    "western_slg": {"label": "SLG大类", "source_mode": "remote"},
    "dlsite": {"label": "DLsite游戏", "source_mode": "bundled_pilot"},
    "all": {"label": "全部游戏", "source_mode": "virtual"},
}


def normalize_catalogue(catalog_id, allow_all=True):
    if not isinstance(catalog_id, str) or catalog_id not in CATALOGUES or (catalog_id == "all" and not allow_all):
        raise ValueError("无法识别的游戏目录。")
    return catalog_id
