"""Personal library data and image operations; no Tk or network dependency.

Callers own SQLite transactions. Image preparation can run in a worker; a
prepared image is detached from its source file and safe to preview afterwards.
"""
import ntpath
import os
import tempfile
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_COVER_PIXELS = 25_000_000
LOCAL_LIBRARY_SQL = "(NULLIF(TRIM(l.folder_path),'') IS NOT NULL OR NULLIF(TRIM(l.exe_path),'') IS NOT NULL OR g.origin='user')"
BACKUP_FIELDS = {
    "cover_overrides": {"slug", "cover_file"},
    "local_bindings": {"slug", "folder_path", "folder_version", "exe_path"},
}


def ensure_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS game_cover_overrides ("
                 "game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,"
                 "cover_file TEXT NOT NULL)")


def _directory(directory=None):
    if directory is None:
        from slg_db import covers_dir
        directory = covers_dir()
    return os.fspath(directory)


def _valid_filename(name):
    return (isinstance(name, str) and bool(name.strip()) and
            name not in (".", "..") and ntpath.basename(name) == name and
            os.path.basename(name) == name and ":" not in name and "\x00" not in name)


def prepare_cover(path):
    """Decode JPEG/PNG/WebP, validate resource limits, and detach an RGB image."""
    try:
        with open(path, "rb") as source:
            if os.fstat(source.fileno()).st_size > MAX_COVER_BYTES:
                raise ValueError("封面图片不能超过 5 MB。")
            with Image.open(source) as opened:
                if opened.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("请选择 JPEG、PNG 或 WebP 图片。")
                if opened.width * opened.height > MAX_COVER_PIXELS:
                    raise ValueError("封面图片不能超过 2500 万像素。")
                opened.load()
                oriented = ImageOps.exif_transpose(opened)
                rgba = oriented.convert("RGBA")
                result = Image.new("RGB", rgba.size, "white")
                result.paste(rgba, mask=rgba.getchannel("A"))
                result.thumbnail((1600, 2400), Image.Resampling.LANCZOS)
                return result
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片无法读取或已损坏。") from exc


def save_cover(image, cover_directory=None):
    """Atomically write a prepared image. Never replace an existing cover."""
    directory = _directory(cover_directory)
    os.makedirs(directory, exist_ok=True)
    name = "user_" + uuid.uuid4().hex + ".jpg"
    descriptor, temporary = tempfile.mkstemp(prefix=".cover-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            image.convert("RGB").save(output, format="JPEG", quality=90)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, os.path.join(directory, name))
        return name
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_cover(path, cover_directory=None):
    image = prepare_cover(path)
    try:
        return save_cover(image, cover_directory)
    finally:
        image.close()


def set_cover_override(conn, game_id, name):
    if not _valid_filename(name):
        raise ValueError("封面缓存文件名无效。")
    ensure_schema(conn)
    conn.execute("INSERT INTO game_cover_overrides(game_id,cover_file) VALUES (?,?)"
                 " ON CONFLICT(game_id) DO UPDATE SET cover_file=excluded.cover_file",
                 (game_id, name))


def clear_cover_override(conn, game_id):
    ensure_schema(conn)
    conn.execute("DELETE FROM game_cover_overrides WHERE game_id=?", (game_id,))


def apply_cover_overrides(conn, rows, cover_directory=None, in_place=False):
    """Batch decorate game dictionaries; unavailable personal images fall back."""
    result = rows if in_place else [dict(row) for row in rows]
    if not result:
        return result
    overrides = {}
    # Read paths must also work on older read-only catalogue snapshots. Schema
    # creation belongs to the writable connect path, never a query decorator.
    has_overrides = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='game_cover_overrides'"
    ).fetchone() is not None
    if has_overrides:
        # This personal table is normally tiny or empty. A single scan avoids
        # allocating thousands of IDs and preparing chunked IN statements on
        # every sort/filter of a large catalogue.
        overrides = dict(conn.execute(
            "SELECT game_id,cover_file FROM game_cover_overrides").fetchall())
    directory = _directory(cover_directory) if overrides else None
    for row in result:
        row["catalogue_cover_file"] = row.get("catalogue_cover_file", row.get("cover_file"))
        name = overrides.get(row["id"])
        available = bool(name and _valid_filename(name) and os.path.isfile(os.path.join(directory, name)))
        row["has_cover_override"] = available
        row["cover_override_missing"] = bool(name and not available)
        row["cover_file"] = name if available else row["catalogue_cover_file"]
    return result


def clear_local_binding(conn, game_id, clear_folder=True, clear_exe=True):
    """Clear registration only. Does not touch status, notes, or filesystem."""
    fields = []
    if clear_folder:
        fields.extend(("folder_path=NULL", "folder_version=NULL"))
    if clear_exe:
        fields.append("exe_path=NULL")
    if fields:
        conn.execute("UPDATE local SET " + ",".join(fields) + " WHERE game_id=?", (game_id,))
        conn.execute("DELETE FROM local WHERE game_id=? AND folder_path IS NULL AND exe_path IS NULL", (game_id,))


def export_sections(conn):
    ensure_schema(conn)
    return {
        "cover_overrides": [dict(row) for row in conn.execute(
            "SELECT g.slug,o.cover_file FROM game_cover_overrides o JOIN games g ON g.id=o.game_id ORDER BY g.slug")],
        "local_bindings": [dict(row) for row in conn.execute(
            "SELECT g.slug,l.folder_path,l.folder_version,l.exe_path FROM local l JOIN games g ON g.id=l.game_id ORDER BY g.slug")],
    }


def validate_sections(data, conn=None):
    """Preflight before restore clears anything; old missing sections are valid."""
    if not isinstance(data, dict):
        raise ValueError("备份内容必须是 JSON 对象。")
    pending_slugs = {row.get("slug") for row in data.get("user_games", []) if isinstance(row, dict)}
    counts = {}
    for section, allowed in BACKUP_FIELDS.items():
        if section not in data:
            continue
        rows = data[section]
        if not isinstance(rows, list):
            raise ValueError("本地游戏库备份节必须是列表。")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - allowed:
                raise ValueError("本地游戏库备份字段无效。")
            slug = row.get("slug")
            if not isinstance(slug, str) or not slug.strip() or slug in seen:
                raise ValueError("本地游戏库备份标识无效或重复。")
            seen.add(slug)
            for key, value in row.items():
                if value is not None and (not isinstance(value, str) or "\x00" in value):
                    raise ValueError("本地游戏库备份字段类型无效。")
            if section == "cover_overrides" and not _valid_filename(row.get("cover_file")):
                raise ValueError("封面缓存文件名无效。")
            if conn is not None and slug not in pending_slugs and conn.execute(
                    "SELECT 1 FROM games WHERE slug=?", (slug,)).fetchone() is None:
                raise ValueError("请先导入本地游戏库备份需要的游戏目录。")
        counts[section] = len(rows)
    return counts


def import_sections(conn, data, cover_directory=None):
    """Restore present sections, rekeying by slug; return missing-image warnings."""
    validate_sections(data, conn)
    ensure_schema(conn)
    warnings = []
    if "cover_overrides" in data:
        conn.execute("DELETE FROM game_cover_overrides")
        directory = _directory(cover_directory)
        for row in data["cover_overrides"]:
            game = conn.execute("SELECT id FROM games WHERE slug=?", (row["slug"],)).fetchone()
            if game is None:
                raise ValueError("游戏目录不存在。")
            if os.path.isfile(os.path.join(directory, row["cover_file"])):
                set_cover_override(conn, game[0], row["cover_file"])
            else:
                warnings.append("个人封面文件缺失，已恢复目录默认封面：" + row["slug"])
    if "local_bindings" in data:
        conn.execute("DELETE FROM local")
        for row in data["local_bindings"]:
            game = conn.execute("SELECT id FROM games WHERE slug=?", (row["slug"],)).fetchone()
            if game is None:
                raise ValueError("游戏目录不存在。")
            conn.execute("INSERT INTO local(game_id,folder_path,folder_version,exe_path) VALUES (?,?,?,?)",
                         (game[0], row.get("folder_path"), row.get("folder_version"), row.get("exe_path")))
    return warnings


def cover_override_conflict(conn, old_id, canonical_id):
    ensure_schema(conn)
    values = {row[0]: row[1] for row in conn.execute(
        "SELECT game_id,cover_file FROM game_cover_overrides WHERE game_id IN (?,?)", (old_id, canonical_id))}
    return bool(old_id in values and canonical_id in values and values[old_id] != values[canonical_id])


def merge_cover_override(conn, old_id, canonical_id):
    """Move only a conflict-free personal cover; conflicting identities stay apart."""
    if cover_override_conflict(conn, old_id, canonical_id):
        raise ValueError("重定向游戏拥有不同的个人封面。")
    conn.execute("INSERT OR IGNORE INTO game_cover_overrides(game_id,cover_file)"
                 " SELECT ?,cover_file FROM game_cover_overrides WHERE game_id=?", (canonical_id, old_id))
    conn.execute("DELETE FROM game_cover_overrides WHERE game_id=?", (old_id,))
