"""Helpers for consistent, compact game names in the client UI."""

from __future__ import annotations

import re
from collections.abc import Mapping


_MISSING = object()


def _field(game, name):
    """Read a field from mappings, sqlite rows, or ordinary data objects."""
    if isinstance(game, str):
        return game if name == "title" else None
    if isinstance(game, Mapping):
        return game.get(name)
    if game is None:
        return None
    try:
        value = game[name]
    except (KeyError, IndexError, TypeError):
        value = _MISSING
    if value is not _MISSING:
        return value
    return getattr(game, name, None)


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def _version_in_title(title, version):
    """Return whether title already contains the same standalone version."""
    core = re.sub(r"^v(?=\d)", "", version.strip(), flags=re.IGNORECASE)
    if not core:
        return False
    # A version is a distinct token; don't treat 1.2 as present in 11.2 or
    # 1.20. The optional v handles both "1.2" and "v1.2" in source titles.
    pattern = r"(?<![A-Za-z0-9.])v?" + re.escape(core) + r"(?![A-Za-z0-9.])"
    return re.search(pattern, title, flags=re.IGNORECASE) is not None


def _developer_in_title(title, developer):
    """Avoid appending a studio already written in the game title."""
    title_folded = title.casefold()
    developer_folded = developer.casefold()
    if not developer_folded:
        return True

    # CJK studio names don't have word boundaries, so an exact substring is
    # the useful signal. For ASCII names, require token boundaries to avoid
    # hiding a developer such as "art" inside an unrelated word.
    if any(ord(char) > 127 for char in developer):
        return developer_folded in title_folded
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(developer_folded) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, title_folded) is not None


def format_game_label(game, title=None):
    """Format a game record as ``title v1.2 · Developer``.

    ``game`` may be a mapping, a ``sqlite3.Row``, an object with matching
    attributes, or a title string. Missing metadata is omitted. The title is
    kept intact (apart from surrounding whitespace), and no metadata is
    inferred from its text.
    """
    requested_title = _text(title)
    title = _text(_field(game, "title")) or "未命名游戏"
    if requested_title:
        title = requested_title
    version = _text(_field(game, "version"))
    developer = _text(_field(game, "developer"))

    parts = [title]
    if version and not _version_in_title(title, version):
        version_label = version if re.match(r"^v(?=\d)", version, re.IGNORECASE) else "v" + version
        parts.append(version_label)
    if developer and not _developer_in_title(title, developer):
        parts.append(developer)
    return " · ".join(parts)
