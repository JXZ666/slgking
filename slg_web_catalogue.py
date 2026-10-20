"""Read-only, paged web access to the two sanitized public catalogues."""
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from slg_cloud_store import CloudError

_DL_SLUG = re.compile(r"dlsite-(?:rj|vj)\d{6,10}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_COVER = re.compile(r"dlsite-((?:RJ|VJ)\d{6,10})-([a-f0-9]{64})\.jpg\Z")
_OFFICIAL_IMAGE = re.compile(r"\.(?:jpg|jpeg|png|webp)\Z", re.I)
_WORK_IN_PATH = re.compile(r"(?<![A-Za-z0-9])((?:RJ|VJ)[0-9]{6,10})(?![0-9])", re.I)
_LOCK = threading.RLock()
_VERIFIED = OrderedDict()
_CATALOGUES = ("western_slg", "dlsite")
_HOSTS = {"dikgames": {"dikgames.com", "www.dikgames.com"},
          "f95zone": {"f95zone.to", "www.f95zone.to", "f95zone.to.it"}, "dlsite": {"www.dlsite.com"}}


def official_cover_url(slug, url):
    """Identity-bound official artwork URL; anything else is not a cover."""
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        return False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "img.dlsite.jp"
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.query
                or "\\" in url or ".." in parsed.path
                or not parsed.path.startswith("/modpub/images2/work/")):
            return False
    except ValueError:
        return False
    if not isinstance(slug, str) or not _DL_SLUG.fullmatch(slug):
        return False
    product = slug[len("dlsite-"):].upper()
    found = {match.group(1).upper() for match in _WORK_IN_PATH.finditer(parsed.path)}
    return bool(_OFFICIAL_IMAGE.search(parsed.path)) and found == {product}


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except (OverflowError, ValueError):
        return None


def safe_source_url(source_id, url, slug=""):
    """Never turn imported provenance into an arbitrary navigation target."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _HOSTS.get(source_id, ())
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or "\\" in url):
            return False
    except ValueError:
        return False
    decoded_path = unquote(parsed.path)
    if (re.search(r"\.(?:zip|rar|7z|exe|torrent|iso)\Z", decoded_path, re.I)
            or re.search(r"/(?:download|downloads|attachments)(?:/|\Z)", decoded_path, re.I)):
        return False
    if source_id == "dlsite":
        if not _DL_SLUG.fullmatch(slug):
            return False
        product = slug[len("dlsite-"):]
        return bool(re.search(r"/product_id/" + re.escape(product) + r"\.html\Z",
                              parsed.path, re.I))
    return bool(parsed.path and parsed.path != "/")


def _safe_path(root, relative):
    path = root / relative
    cursor = path
    while True:
        if cursor.is_symlink():
            raise CloudError("catalogue unavailable", 503)
        if cursor == root:
            break
        if cursor.parent == cursor:
            raise CloudError("catalogue unavailable", 503)
        cursor = cursor.parent
    if not path.is_file():
        raise CloudError("catalogue unavailable", 503)
    return path


def _verified_file(path, digest, *, json_value=False):
    stat = path.stat()
    signature = (str(path), digest, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    with _LOCK:
        if signature in _VERIFIED:
            _VERIFIED.move_to_end(signature)
            return _VERIFIED[signature]
        if stat.st_size > (32 * 1024 * 1024 if json_value else 256 * 1024 * 1024):
            raise CloudError("catalogue unavailable", 503)
        hasher = hashlib.sha256()
        chunks = [] if json_value else None
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                hasher.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
        if hasher.hexdigest() != digest:
            raise CloudError("catalogue unavailable", 503)
        value = json.loads(b"".join(chunks)) if json_value else True
        _VERIFIED[signature] = value
        while len(_VERIFIED) > 8:
            _VERIFIED.popitem(last=False)
        return value


def _uri(path, immutable=False):
    return "file:" + quote(str(path).replace(os.sep, "/"), safe="/:") + "?mode=ro" + ("&immutable=1" if immutable else "")


class WebCatalogue:
    def __init__(self, publish_dir):
        self.root = Path(os.path.abspath(publish_dir))

    def _snapshot(self):
        manifest_path = _safe_path(self.root, "catalogues/dlsite/manifest.json")
        if manifest_path.stat().st_size > 65536:
            raise CloudError("catalogue unavailable", 503)
        manifest = json.loads(manifest_path.read_bytes())
        if not isinstance(manifest, dict) or manifest.get("catalog_id") != "dlsite":
            raise CloudError("catalogue unavailable", 503)
        paths = []
        for hash_key, path_key, folder, suffix in (
                ("db_sha256", "db_path", "snapshots", ".db"),
                ("cover_index_sha256", "cover_index_path", "cover-index", ".json")):
            digest = manifest.get(hash_key)
            if not isinstance(digest, str) or not _HASH.fullmatch(digest):
                raise CloudError("catalogue unavailable", 503)
            relative = "catalogues/dlsite/" + folder + "/" + digest + suffix
            if manifest.get(path_key) != "/" + relative:
                raise CloudError("catalogue unavailable", 503)
            paths.append(_safe_path(self.root, relative))
        _verified_file(paths[0], manifest["db_sha256"])
        covers = _verified_file(paths[1], manifest["cover_index_sha256"], json_value=True)
        if not isinstance(covers, dict):
            raise CloudError("catalogue unavailable", 503)
        official = self._official_covers(manifest)
        return paths[0], covers, official

    def _official_covers(self, manifest):
        """Optional official-cover index: slug -> official artwork URL.

        The index is additive: a manifest without the official keys (older
        publisher) yields no cover_url values, while an incomplete or corrupt
        declared index falls back to the still-verified legacy cover index
        instead of taking the whole catalogue down.
        """
        keys = {"official_cover_index_sha256", "official_cover_index_path",
                "official_cover_delivery"}
        if not keys & set(manifest):
            return {}
        try:
            return self._verified_official_covers(manifest)
        except (CloudError, OSError, ValueError, TypeError):
            return {}

    def _verified_official_covers(self, manifest):
        digest = manifest.get("official_cover_index_sha256")
        declared = manifest.get("official_cover_index_path")
        if (not isinstance(digest, str) or not _HASH.fullmatch(digest)
                or not isinstance(declared, str)
                or manifest.get("official_cover_delivery") != "client-direct"):
            raise ValueError("incomplete official cover manifest")
        relative = "catalogues/dlsite/official-cover-index/" + digest + ".json"
        if declared != "/" + relative:
            raise ValueError("unexpected official cover route")
        payload = _verified_file(_safe_path(self.root, relative), digest, json_value=True)
        if (not isinstance(payload, dict) or type(payload.get("version")) is not int
                or payload["version"] != 1 or payload.get("catalog_id") != "dlsite"
                or not isinstance(payload.get("items"), dict)):
            raise ValueError("invalid official cover index")
        covers = {}
        for slug, entry in payload["items"].items():
            if not isinstance(slug, str) or not _DL_SLUG.fullmatch(slug):
                continue
            url = entry if isinstance(entry, str) else entry.get("url") if isinstance(entry, dict) else None
            if official_cover_url(slug, url):
                covers[slug] = url
        return covers

    @contextmanager
    def _open(self, catalogue):
        conn = None
        try:
            selected = _CATALOGUES if catalogue == "all" else (catalogue,)
            paths, covers, official = {}, {}, {}
            for kind in selected:
                if kind == "dlsite":
                    paths[kind], covers, official = self._snapshot()
                else:
                    paths[kind] = _safe_path(self.root, "slgking.db")
            conn = sqlite3.connect(":memory:", timeout=3, uri=True)
            conn.row_factory = sqlite3.Row
            for kind, path in paths.items():
                conn.execute("ATTACH DATABASE ? AS " + kind,
                             (_uri(path, kind == "dlsite"),))
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn, covers, official, selected
        except CloudError:
            raise
        except (OSError, ValueError, TypeError, sqlite3.Error):
            raise CloudError("catalogue unavailable", 503) from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _kind(value, all_allowed=True):
        if value not in (_CATALOGUES + (("all",) if all_allowed else ())):
            raise CloudError("bad query")
        return value

    @staticmethod
    def _columns(conn, kind, table):
        return {row[1] for row in conn.execute("PRAGMA " + kind + ".table_info(" + table + ")")}

    def _selection(self, conn, kind, text="", tags=(), languages=(), types=()):
        columns = self._columns(conn, kind, "games")
        clauses = ["g.origin='site'"] if "origin" in columns else []
        if kind == "western_slg":
            clauses.append("g.slug NOT LIKE 'dlsite-%'")
        else:
            clauses.append("g.slug LIKE 'dlsite-%'")
        args = []
        if text:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            term = "%" + escaped + "%"
            fields = ["g.title"] + (["g.developer", "g.slug"] if kind == "dlsite" else [])
            clauses.append("(" + " OR ".join(field + " LIKE ? ESCAPE '\\'" for field in fields) + ")")
            args.extend([term] * len(fields))
        if tags:
            marks = ",".join("?" for _ in tags)
            clauses.append("(SELECT COUNT(DISTINCT t.name) FROM " + kind + ".game_tags gt JOIN "
                           + kind + ".tags t ON t.id=gt.tag_id WHERE gt.game_id=g.id AND t.name IN ("
                           + marks + "))=?")
            args.extend(tags)
            args.append(len(tags))
        metadata = "'{}'"
        if kind == "dlsite" and self._columns(conn, kind, "game_metadata"):
            metadata = "COALESCE((SELECT CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END FROM dlsite.game_metadata dm WHERE dm.game_id=g.id),'{}')"
        for values, field in ((languages, "languages"), (types, "work_type")):
            if not values:
                continue
            known = [value for value in values if value != "__unknown__"]
            if field == "languages":
                array = "CASE WHEN json_type(" + metadata + ",'$.languages')='array' THEN json_extract(" + metadata + ",'$.languages') ELSE '[]' END"
                present = "EXISTS(SELECT 1 FROM json_each(" + array + ") j WHERE j.type='text' AND trim(j.value)!='')"
                condition = "EXISTS(SELECT 1 FROM json_each(" + array + ") j WHERE j.type='text' AND trim(j.value) IN (" + ",".join("?" for _ in known) + "))" if known else "0"
            else:
                expression = "CASE WHEN json_type(" + metadata + ",'$.work_type')='text' THEN trim(json_extract(" + metadata + ",'$.work_type')) ELSE '' END"
                present = expression + "!=''"
                condition = expression + " IN (" + ",".join("?" for _ in known) + ")" if known else "0"
            if "__unknown__" in values:
                condition = "(" + condition + " OR NOT(" + present + "))"
            clauses.append(condition)
            args.extend(known)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        fields = []
        for name in ("id", "slug", "title", "developer", "engine", "version", "rating", "last_updated", "cover_file", "heat", "overview"):
            expression = "g." + name if name in columns else "NULL"
            if kind == "dlsite" and name == "rating":
                expression = "NULL"
            fields.append(expression + " AS " + name)
        score = "json_extract(" + metadata + ",'$.rating_value')"
        fields.extend(["'" + kind + "' AS catalog_id", "CASE WHEN json_type(" + metadata + ",'$.rating_value') IN ('real','integer') AND " + score + ">=0 AND " + score + "<=1.7976931348623157e308 THEN " + score + " END AS source_rating"])
        for field in ("rating_scale_max", "rating_count"):
            value = "json_extract(" + metadata + ",'$." + field + "')"
            fields.append("CASE WHEN json_type(" + metadata + ",'$." + field + "') IN ('real','integer') AND " + value + ">=0 AND " + value + "<=1.7976931348623157e308 THEN " + value + " END AS source_" + field)
        return "SELECT " + ",".join(fields) + " FROM " + kind + ".games g" + where, args

    @staticmethod
    def _values(query, key, maximum=8):
        result = []
        for value in query.get(key, []):
            value = value.strip()
            if not value or len(value) > 100:
                raise CloudError("bad query")
            if value not in result:
                result.append(value)
        if len(result) > maximum:
            raise CloudError("bad query")
        return result

    def search(self, query):
        allowed = {"q", "tag", "sort", "page", "limit", "catalog_id", "language", "work_type"}
        if set(query) - allowed or any(len(values) != 1 for key, values in query.items() if key not in {"tag", "language", "work_type"}):
            raise CloudError("bad query")
        kind = self._kind(query.get("catalog_id", ["western_slg"])[0])
        text = query.get("q", [""])[0].strip()
        if len(text) > 100:
            raise CloudError("bad query")
        tags = self._values(query, "tag")
        if any(len(tag) > 40 for tag in tags):
            raise CloudError("bad query")
        languages, types = self._values(query, "language"), self._values(query, "work_type")
        if kind != "dlsite" and (languages or types):
            raise CloudError("bad query")
        sort = query.get("sort", ["heat"])[0]
        orders = {"heat": "heat DESC", "rating": "rating IS NULL,rating DESC,heat DESC",
                  "updated": "last_updated IS NULL,last_updated DESC", "title": "title COLLATE NOCASE ASC",
                  "source_rating": "source_rating IS NULL,source_rating DESC,title COLLATE NOCASE ASC"}
        if sort not in orders or (sort == "source_rating" and kind != "dlsite"):
            raise CloudError("bad query")
        try:
            page, limit = int(query.get("page", ["1"])[0]), int(query.get("limit", ["20"])[0])
        except (ValueError, TypeError):
            raise CloudError("bad query") from None
        if not 1 <= page <= 1000 or not 1 <= limit <= 50:
            raise CloudError("bad query")
        with self._open(kind) as (conn, covers, official, selected):
            branches = [self._selection(conn, k, text, tags, languages, types) for k in selected]
            sql = " UNION ALL ".join(branch[0] for branch in branches)
            args = [value for branch in branches for value in branch[1]]
            total = conn.execute("SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
            rows = conn.execute("SELECT * FROM (" + sql + ") ORDER BY " + orders[sort] + ",catalog_id,slug LIMIT ? OFFSET ?", args + [limit, (page - 1) * limit]).fetchall()
            items = self._items(conn, rows, covers, official)
        return {"ok": True, "page": page, "limit": limit, "total": total,
                "has_more": page * limit < total, "items": items}

    def _items(self, conn, rows, covers, official=None):
        official = official or {}
        tag_map = {}
        for kind in _CATALOGUES:
            ids = [row["id"] for row in rows if row["catalog_id"] == kind]
            if not ids:
                continue
            for row in conn.execute("SELECT gt.game_id,t.name FROM " + kind + ".game_tags gt JOIN " + kind + ".tags t ON t.id=gt.tag_id WHERE gt.game_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY t.name", ids):
                tag_map.setdefault((kind, row[0]), []).append(row[1])
        items = []
        for row in rows:
            kind, filename = row["catalog_id"], row["cover_file"]
            if kind == "dlsite":
                indexed = covers.get(row["slug"], {})
                match = _COVER.fullmatch(str(filename or ""))
                if (not isinstance(indexed, dict) or not match or indexed.get("filename") != filename
                        or indexed.get("sha256") != match[2] or row["slug"] != "dlsite-" + match[1].lower()):
                    filename = None
            if filename and (Path(filename).name != filename or "\\" in filename):
                filename = None
            if filename:
                try:
                    _safe_path(self.root, "covers/" + filename)
                except CloudError:
                    filename = None
            items.append({"slug": row["slug"], "title": row["title"],
                          "developer": row["developer"] or "", "engine": row["engine"] or "",
                          "version": row["version"] or "", "rating": row["rating"],
                          "updated": row["last_updated"] or "", "cover": "/covers/" + filename if filename else None,
                          "cover_url": official.get(row["slug"]),
                          "tags": tag_map.get((kind, row["id"]), []), "catalog_id": kind})
            if kind == "dlsite":
                items[-1]["facts"] = {"rating_value": _number(row["source_rating"]),
                                      "rating_scale_max": _number(row["source_rating_scale_max"]),
                                      "rating_count": _number(row["source_rating_count"])}
        return items

    def tags(self, catalog_id="western_slg"):
        kind = self._kind(catalog_id)
        with self._open(kind) as (conn, _, _, selected):
            branches = []
            for source in selected:
                clause = " WHERE g.origin='site'" if "origin" in self._columns(conn, source, "games") else ""
                branches.append("SELECT t.name AS slug,COUNT(DISTINCT g.id) AS count FROM " + source + ".tags t JOIN " + source + ".game_tags gt ON gt.tag_id=t.id JOIN " + source + ".games g ON g.id=gt.game_id" + clause + " GROUP BY t.id,t.name")
            rows = conn.execute("SELECT slug,SUM(count) AS count FROM (" + " UNION ALL ".join(branches) + ") GROUP BY slug ORDER BY count DESC,slug COLLATE NOCASE").fetchall()
        try:
            labels = json.loads((Path(__file__).parent / "assets/tag_zh.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            labels = {}
        labels = labels if isinstance(labels, dict) else {}
        def label(slug):
            value = labels.get(slug)
            return value if isinstance(value, str) and value.strip() else slug
        return {"ok": True, "tags": [{"slug": row["slug"], "label": label(row["slug"]), "count": row["count"]} for row in rows]}

    def filters(self, catalog_id="dlsite"):
        if catalog_id != "dlsite":
            raise CloudError("bad query")
        with self._open("dlsite") as (conn, _, _, _):
            if self._columns(conn, "dlsite", "game_metadata"):
                safe = "CASE WHEN json_valid(dm.metadata_json) THEN dm.metadata_json ELSE '{}' END"
                join = " FROM dlsite.game_metadata dm JOIN dlsite.games g ON g.id=dm.game_id WHERE g.origin='site'"
                array = "CASE WHEN json_type(" + safe + ",'$.languages')='array' THEN json_extract(" + safe + ",'$.languages') ELSE '[]' END"
                languages = [row[0] for row in conn.execute("SELECT DISTINCT trim(j.value) FROM dlsite.game_metadata dm JOIN dlsite.games g ON g.id=dm.game_id JOIN json_each(" + array + ") j WHERE g.origin='site' AND j.type='text' AND trim(j.value)!='' ORDER BY 1 COLLATE NOCASE")]
                types = [row[0] for row in conn.execute("SELECT DISTINCT trim(json_extract(" + safe + ",'$.work_type'))" + join + " AND typeof(json_extract(" + safe + ",'$.work_type'))='text' AND trim(json_extract(" + safe + ",'$.work_type'))!='' ORDER BY 1 COLLATE NOCASE")]
            else:
                languages, types = [], []
        options = lambda values: [{"value": value, "label": value} for value in values] + [{"value": "__unknown__", "label": "未知"}]
        return {"ok": True, "catalog_id": "dlsite", "languages": options(languages), "work_types": options(types)}

    def _resolve(self, conn, kind, slug):
        if not self._columns(conn, kind, "game_redirects"):
            return slug, [slug]
        current, seen = slug, set()
        for _ in range(100):
            if current in seen:
                return slug, [slug]
            seen.add(current)
            row = conn.execute("SELECT canonical_slug FROM " + kind + ".game_redirects WHERE old_slug=?", (current,)).fetchone()
            if not row:
                break
            target = str(row[0])
            if bool(_DL_SLUG.fullmatch(target)) != (kind == "dlsite"):
                return slug, [slug]
            current = target
        else:
            return slug, [slug]
        rows = conn.execute("WITH RECURSIVE related(slug) AS (SELECT ? UNION SELECT r.old_slug FROM " + kind + ".game_redirects r JOIN related x ON r.canonical_slug=x.slug) SELECT slug FROM related", (current,)).fetchall()
        return current, sorted({slug, current} | {row[0] for row in rows if bool(_DL_SLUG.fullmatch(row[0])) == (kind == "dlsite")})

    def game_slugs(self, slug):
        if not isinstance(slug, str) or not slug.strip() or len(slug) > 200:
            raise CloudError("not found", 404)
        slug = slug.strip()
        kind = "dlsite" if _DL_SLUG.fullmatch(slug) else "western_slg"
        with self._open(kind) as (conn, _, _, _):
            return self._resolve(conn, kind, slug)

    def canonical_slug(self, slug):
        return self.game(slug)["game"]["slug"]

    def game(self, slug, catalog_id=None):
        if not isinstance(slug, str) or not slug.strip() or len(slug) > 200:
            raise CloudError("not found", 404)
        slug = slug.strip()
        kind = "dlsite" if _DL_SLUG.fullmatch(slug) else "western_slg"
        if catalog_id is not None and self._kind(catalog_id) not in (kind, "all"):
            raise CloudError("not found", 404)
        with self._open(kind) as (conn, covers, official, _):
            canonical, _ = self._resolve(conn, kind, slug)
            select, _ = self._selection(conn, kind)
            row = conn.execute("SELECT * FROM (" + select + ") WHERE slug IN (?,?) ORDER BY slug LIMIT 1", (canonical, unquote(canonical))).fetchone()
            if row is None:
                raise CloudError("not found", 404)
            item = self._items(conn, [row], covers, official)[0]
            item["overview"] = row["overview"] or ""
            sources = []
            if self._columns(conn, kind, "game_sources"):
                for source in conn.execute("SELECT source_id,source_url,source_title FROM " + kind + ".game_sources WHERE game_id=? ORDER BY source_id", (row["id"],)):
                    if safe_source_url(source[0], source[1], row["slug"]):
                        sources.append({"source_id": source[0], "source_url": source[1], "source_title": source[2] or ""})
            item["sources"] = sources
            if kind == "dlsite":
                metadata = {}
                if self._columns(conn, kind, "game_metadata"):
                    raw = conn.execute("SELECT metadata_json FROM dlsite.game_metadata WHERE game_id=?", (row["id"],)).fetchone()
                    try:
                        metadata = json.loads(raw[0]) if raw else {}
                    except (ValueError, TypeError):
                        pass
                metadata = metadata if isinstance(metadata, dict) else {}
                facts = {"product_id": row["slug"][len("dlsite-"):].upper()}
                for name in ("release_date", "updated_date", "work_type", "age_rating"):
                    value = metadata.get(name)
                    facts[name] = value if isinstance(value, str) and value.strip() else None
                values = metadata.get("languages")
                facts["languages"] = [value for value in values if isinstance(value, str) and value.strip()] if isinstance(values, list) else []
                for name in ("rating_value", "rating_count", "rating_scale_max"):
                    value = metadata.get(name)
                    facts[name] = _number(value)
                item["facts"] = facts
        return {"ok": True, "game": item}
