"""slgking 安卓进程内 JSON 桥（Kotlin Chaquopy 互操作入口）。

契约（见 openspec platform-bridge 规格）：
  - Kotlin 侧调用 ``slg_android_api.call(json_string) -> json_string``
  - 请求体 ``{"method": "<name>", ...参数}``；响应统一 ``{"ok": true, ...}``
    或 ``{"ok": false, "error": "<中文可读信息>"}``，异常不跨桥。
  - 长任务（同步/翻译）以 job_id + poll 轮询，不在桥调用内长阻塞。
  - 当前共 63 个端点（目录/详情/库/同步/账号/评论/游戏化/工具/遥测）。

使用前必须先完成 ``slg_android_bootstrap.init(...)``（ping 会校验）。
桌面冒烟：tools/smoke_android_api.py 以临时数据目录离线驱动本模块。
"""

import json
import os
import threading

BRIDGE_VERSION = "1.0"

_TLS = threading.local()
_INIT_LOCK = threading.RLock()
_AUTH_LOCK = threading.RLock()
_SESSION_EPOCH = 0


def _boot():
    import slg_android_bootstrap
    if not slg_android_bootstrap._state["initialized"]:
        raise RuntimeError("bootstrap 未初始化：请先调用 slg_android_bootstrap.init")
    return slg_android_bootstrap


def _conn():
    """每线程一个连接（上游线程模型：sqlite 连接不跨线程，WAL 并发读）。"""
    import slg_db
    conn = getattr(_TLS, "conn", None)
    if conn is None:
        conn = slg_db.connect()
        _TLS.conn = conn
    return conn


def _close_conn():
    conn = getattr(_TLS, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _TLS.conn = None


def _err(message):
    return {"ok": False, "error": str(message)}


def _page(req, default_size=30, max_size=80):
    try:
        page = max(1, int(req.get("page", 1)))
        size = max(1, min(max_size, int(req.get("page_size", default_size))))
    except (TypeError, ValueError):
        page, size = 1, default_size
    return page, size, (page - 1) * size


def _cover_path(cover_file):
    if not cover_file:
        return None
    import os

    import slg_db
    path = os.path.join(slg_db.covers_dir(), cover_file)
    return path if os.path.exists(path) else None


def _title_zh_map(conn):
    import slg_db
    return {int(k): v[0] for k, v in slg_db.title_translations(conn).items()
            if slg_db._int_or_none(k) is not None}


def _tag_zh_map(conn):
    import slg_db
    return slg_db.tag_translations(conn)


_SORTS = {"score": "score", "rating": "rating", "updated": "updated",
          "title": "title", "heat": "heat"}


def _query_rows(req):
    """按请求组装 find_games 参数（对齐 slg_gui 列表查询语义）。"""
    import slg_db

    conn = _conn()
    view = str(req.get("view") or "all")
    kwargs = {"sort": _SORTS.get(str(req.get("sort") or "score"), "score"),
              "desc": bool(req.get("desc", True))}
    from slg_catalogues import normalize_catalogue
    catalog_id = normalize_catalogue(req.get("catalog_id", "all" if view in ("user", "collection") else "western_slg"))
    kwargs.update(catalog_id=catalog_id, dlsite_languages=req.get("languages", []),
                  dlsite_work_types=req.get("work_types", []))
    if catalog_id == "dlsite" and kwargs["sort"] in ("score", "rating", "heat"):
        kwargs["sort"] = "source_rating"
    elif catalog_id == "all" and kwargs["sort"] in ("score", "rating", "heat"):
        kwargs["sort"] = "title"
    if view == "user":
        kwargs["origin"] = "user"
        kwargs["category_ids"] = req.get("category_ids", [])
        kwargs["uncategorized"] = bool(req.get("uncategorized", False))
    elif view == "collection":
        kwargs["origin"] = None
        kwargs["collection_id"] = int(req.get("collection_id", 0))
    else:
        kwargs["origin"] = "main"
        if view == "want":
            kwargs["statuses"] = ["want"]
        elif view == "downloaded":
            kwargs["statuses"] = ["downloaded"]

    q = str(req.get("q") or "").strip()
    if q:
        kwargs["search"] = q
        kwargs["translation_ids"] = slg_db.title_translation_game_ids(conn, q)

    include = req.get("tags") or []
    exclude = req.get("exclude") or []
    if include:
        kwargs["include"] = [str(t) for t in include]
    if exclude:
        kwargs["exclude"] = [str(t) for t in exclude]
    return slg_db.find_games(conn, **kwargs), conn


def _is_dlsite(row):
    """RJ (maniax) and VJ (pro) are both site games in the DLsite catalogue."""
    return ("dlsite" in row.get("catalog_ids", ())
            or str(row.get("slug") or "").startswith("dlsite-"))


def _official_cover_index(conn):
    """Read-only official references; a corrupt pref must not break browsing."""
    import slg_dlsite_online
    try:
        return slg_dlsite_online.get_official_index(conn)
    except Exception:
        return {}


def _item(row, title_zh, official=None):
    row = dict(row)
    official = official or {}
    dlsite = _is_dlsite(row)
    metadata = row.get("source_metadata") or {}
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "title_zh": title_zh.get(row["id"]),
        "rating": metadata.get("rating_value") if dlsite else row["rating"],
        "status": row.get("status"),
        "my_rating": row.get("my_rating"),
        "version": row["version"],
        "developer": row["developer"],
        "engine": row["engine"],
        "cover": _cover_path(row["cover_file"]),
        "cover_url": (official.get(row["slug"]) or {}).get("url") or "",
        "last_updated": row["last_updated"],
        "categories": list(row.get("categories", ())),
        "catalog_id": "dlsite" if dlsite else "western_slg",
        "cover_file": row.get("cover_file"),
        "has_cover_override": bool(row.get("has_cover_override")),
        "rating_count": metadata.get("rating_count"),
        "rating_scale_max": metadata.get("rating_scale_max"),
    }


# --- 基础 ------------------------------------------------------------------


def _h_ping(req):
    _boot()
    import sys
    return {"bridge": BRIDGE_VERSION, "python": sys.version.split()[0]}


def _h_stats(req):
    import slg_android_bootstrap
    _boot()
    conn = _conn()
    out = dict(slg_android_bootstrap.status())
    out.pop("asset_root", None)
    out.update(slg_db_stats(conn))
    return out


def slg_db_stats(conn):
    import slg_db
    out = dict(slg_db.stats(conn))
    out["collections"] = len(slg_db.list_collections(conn))
    return out


# --- 目录 / 详情 / 库 -------------------------------------------------------


def _h_catalog_list(req):
    rows, conn = _query_rows(req)
    title_zh = _title_zh_map(conn)
    official = _official_cover_index(conn)
    page, size, offset = _page(req)
    items = [_item(r, title_zh, official) for r in rows[offset:offset + size]]
    return {"items": items, "total": len(rows), "page": page, "page_size": size}


def _h_game_get(req):
    import slg_db
    conn = _conn()
    game_id = req.get("id")
    if game_id is None and req.get("slug"):
        row0 = conn.execute("SELECT id FROM games WHERE slug=?",
                            (str(req["slug"]),)).fetchone()
        game_id = row0["id"] if row0 else None
    if game_id is None:
        return _err("缺少游戏 id/slug")
    row = slg_db.get_game(conn, int(game_id))
    if row is None:
        return _err("游戏不存在")
    row["source_metadata"] = slg_db.get_game_metadata_bulk(conn, [row["id"]]).get(row["id"], {})
    state = conn.execute(
        "SELECT status, my_rating, note FROM state WHERE game_id=?",
        (row["id"],)).fetchone()
    zh_tags = _tag_zh_map(conn)
    tags = [{"name": name, "zh": zh_tags.get(name) or name}
            for name in slg_db.game_tags(conn, row["id"])]
    item = _item(row, _title_zh_map(conn), _official_cover_index(conn))
    item["status"] = state["status"] if state else None
    item["my_rating"] = state["my_rating"] if state else None
    item.update({
        "categories": list(slg_db.game_categories(conn, row["id"])),
        "overview": row["overview"],
        "url": row["url"],
        "origin": row["origin"],
        "promoted": bool(row["promoted"]),
        "site_views": row["site_views"],
        "site_likes": row["site_likes"],
        "site_comments": row["site_comments"],
        "note": state["note"] if state else None,
        "tags": tags,
        "collections": slg_db.collections_for_game(conn, row["id"]),
        "sources": _game_sources(conn, row),
        "dlsite_facts": __import__("slg_dlsite_client").detail_facts(row) if _is_dlsite(row) else None,
    })
    return {"game": item}


def _h_game_rate(req):
    import slg_db
    conn = _conn()
    game_id = int(req.get("id", 0))
    stars = req.get("stars", None)
    if stars in (None, ""):
        return _err("缺少 stars")
    if stars is False or stars == 0:
        slg_db.set_state(conn, game_id, my_rating="")
    else:
        stars = max(1, min(5, int(stars)))
        slg_db.set_state(conn, game_id, my_rating=stars)
    slg_db.recompute_weights(conn)
    return {"my_rating": stars if stars else None}


def _personal_source_url(value):
    from urllib.parse import urlsplit
    if not isinstance(value, str):
        raise ValueError("来源网址必须是文字")
    value = value.strip()
    if not value:
        return ""
    p = urlsplit(value)
    if (len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value) or
            p.scheme.lower() not in ("http", "https") or not p.hostname or p.username or p.password):
        raise ValueError("请输入有效 HTTP/HTTPS 来源网址")
    p.port
    return value


def _game_sources(conn, row):
    import slg_db
    if row["origin"] == "user":
        try:
            url = _personal_source_url(row.get("url") or "")
        except ValueError:
            return []
        return [{"source_id": "personal", "label": "记录来源", "url": url}] if url else []
    sources = slg_db.list_game_sources(conn, row["id"])
    if not sources:
        for source in ("dlsite", "dikgames", "f95zone"):
            url = slg_db._safe_source_url(source, row.get("url"))
            if url:
                sources = [{"source_id": source, "label": source, "url": url}]
                break
    seen = set()
    return [s for s in sources if not (s["source_id"] in seen or seen.add(s["source_id"]))]


def _h_dlsite_filters(req):
    import slg_dlsite_client
    return slg_dlsite_client.filter_options(_conn())


_COVER_CACHES = {}
_COVER_LOCK = threading.Lock()
_INDEX_LOCK = threading.Lock()
_INDEX_RETRIES = {}


def _h_cover_get(req):
    import slg_db, slg_sync_server, slg_dlsite_online
    row = slg_db.get_game(_conn(), int(req.get("id", 0)))
    if not row:
        return _err("游戏不存在")
    if row["origin"] == "user":
        return {"path": _cover_path(row.get("cover_file"))}
    directory = slg_db.covers_dir()
    def fetch(path):
        return slg_sync_server._server_http_get(slg_sync_server.SERVER_BASE + path, timeout=30)
    dlsite = _is_dlsite(row)
    key = (directory, dlsite)
    with _COVER_LOCK:
        if key not in _COVER_CACHES:
            from slg_public_cover_cache import PublicCoverCache
            _COVER_CACHES[key] = slg_dlsite_online.CoverCache(directory, fetch) if dlsite else PublicCoverCache(directory, fetch)
        cache = _COVER_CACHES[key]
    if dlsite:
        index = slg_dlsite_online.get_cover_index(_conn())
        if not index:
            import hashlib, time
            with _INDEX_LOCK:
                index = slg_dlsite_online.get_cover_index(_conn())
                if not index and (req.get("retry") or time.monotonic() >= _INDEX_RETRIES.get(directory, 0)):
                    try:
                        manifest = slg_dlsite_online.validate_manifest(json.loads(fetch(slg_dlsite_online.MANIFEST_PATH)))
                        raw = fetch(manifest["cover_index_path"])
                        if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != manifest["cover_index_sha256"]:
                            raise ValueError("封面索引校验失败")
                        index = slg_dlsite_online.validate_cover_index(json.loads(raw))
                        with _INIT_LOCK:
                            slg_db.set_pref(_conn(), slg_dlsite_online.INDEX_KEY, json.dumps(index))
                            _conn().commit()
                    except Exception:
                        _INDEX_RETRIES[directory] = time.monotonic() + 30
        entry = index.get(row["slug"])
        if not entry:
            return {"path": None, "unavailable": True}
        if req.get("retry"):
            with cache._lock:
                cache._failure.pop(entry["filename"], None)
        return {"path": cache.get(entry)}
    if req.get("retry"):
        with cache._lock:
            cache._failure.pop(row.get("cover_file"), None)
    return {"path": cache.get(row.get("cover_file"))} if row.get("cover_file") else {"path": None}


def _h_game_state(req):
    import slg_db
    conn = _conn()
    status = req.get("status", None)
    if status not in (None, "", "want", "downloaded"):
        return _err("非法 status: %r" % status)
    slg_db.set_state(conn, int(req.get("id", 0)), status=status or "")
    return {"status": status or None}


def _h_game_note(req):
    import slg_db
    conn = _conn()
    note = req.get("note")
    if note is None:
        return _err("缺少 note")
    conn.execute("INSERT OR IGNORE INTO state (game_id) VALUES (?)",
                 (int(req.get("id", 0)),))
    conn.execute("UPDATE state SET note=?, updated_at=datetime('now')"
                 " WHERE game_id=?", (str(note), int(req.get("id", 0))))
    conn.commit()
    return {"note": str(note)}


def _h_collections_list(req):
    import slg_db
    return {"collections": slg_db.list_collections(_conn())}


def _h_collection_save(req):
    """新建/重命名收藏夹：name 必填；id 提供时为重命名。"""
    conn = _conn()
    name = str(req.get("name") or "").strip()
    if not name:
        return _err("收藏夹名称不能为空")
    if req.get("id"):
        row = conn.execute("SELECT id FROM collections WHERE name=? AND id<>?",
                           (name, int(req["id"]))).fetchone()
        if row:
            return _err("已有同名收藏夹")
        conn.execute("UPDATE collections SET name=? WHERE id=?",
                     (name, int(req["id"])))
        conn.commit()
        return {"id": int(req["id"]), "name": name}
    import slg_db
    cid = slg_db.create_collection(conn, name)
    return {"id": cid, "name": name}


def _h_collection_delete(req):
    import slg_db
    cid = int(req.get("id", 0))
    conn = _conn()
    name_row = conn.execute("SELECT name FROM collections WHERE id=?",
                            (cid,)).fetchone()
    if name_row and name_row["name"] == "收藏夹":
        return _err("默认收藏夹不可删除")
    slg_db.delete_collection(conn, cid)
    return {}


def _h_game_collections_set(req):
    import slg_db
    ids = [int(x) for x in (req.get("collection_ids") or [])]
    slg_db.set_game_collections(_conn(), int(req.get("id", 0)), ids)
    return {"collections": ids}


def _h_tags_list(req):
    import slg_db
    conn = _conn()
    zh = _tag_zh_map(conn)
    limit = req.get("limit")
    rows = slg_db.tag_counts(conn, int(limit) if limit else None, catalog_id=req.get("catalog_id", "western_slg"))
    return {"tags": [{"name": r["name"], "zh": zh.get(r["name"]) or r["name"],
                      "count": r["n"]} for r in rows]}


def _h_usergames_add(req):
    import slg_db
    title = str(req.get("title") or "").strip()
    if not title:
        return _err("标题不能为空")
    source_url = _personal_source_url(req.get("source_url", ""))
    game_id = slg_db.add_user_game(
        _conn(), title, developer=req.get("developer") or None,
        engine=req.get("engine") or None, version=req.get("version") or None,
        overview=req.get("overview") or None,
        tags=[str(t) for t in (req.get("tags") or [])],
        promoted=1 if req.get("promoted") else 0,
        categories=req.get("categories", []))
    if source_url:
        _conn().execute("UPDATE games SET url=? WHERE id=?", (source_url, game_id))
    return {"id": game_id}


def _h_usergames_update(req):
    import slg_db
    conn = _conn()
    game_id = int(req.get("id", 0))
    row = conn.execute("SELECT origin,overview,promoted FROM games WHERE id=?", (game_id,)).fetchone()
    if row is None or row["origin"] != "user":
        return _err("只能编辑我添加的游戏")
    source_url = _personal_source_url(req["source_url"]) if "source_url" in req else None
    slg_db.update_user_game(
        conn, game_id, title=str(req.get("title") or "").strip(),
        developer=req.get("developer") or None, engine=req.get("engine") or None,
        version=req.get("version") or None,
        overview=req.get("overview") if "overview" in req else row["overview"],
        tags=[str(t) for t in req["tags"]] if "tags" in req else slg_db.game_tags(conn, game_id),
        promoted=int(bool(req["promoted"])) if "promoted" in req else row["promoted"],
        categories=req.get("categories"))
    if source_url is not None:
        conn.execute("UPDATE games SET url=? WHERE id=?", (source_url or "user://" + str(game_id), game_id))
    conn.commit()
    return {}


def _h_usergames_delete(req):
    import slg_db
    conn = _conn()
    game_id = int(req.get("id", 0))
    row = conn.execute("SELECT origin FROM games WHERE id=?", (game_id,)).fetchone()
    if row is None or row["origin"] != "user":
        return _err("只能删除我添加的游戏")
    slg_db.delete_game(conn, game_id)
    conn.commit()
    return {}


# --- 同步（后台 job + 轮询）--------------------------------------------------

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_SYNC_JOB = {"id": None}


def _job_snapshot(job):
    return {"job": job["id"], "running": job["running"], "text": job["text"],
            "progress": job["progress"], "summary": job["summary"],
            "error": job["error"], "log": job["log"][-12:],
            "cancel_requested": job.get("cancel_requested", False),
            "cancelled": job.get("cancelled", False),
            "catalog_updated": job.get("catalog_updated", False),
            "stage": job.get("stage")}


def _sync_worker(job):
    import slg_db
    import slg_sync_server

    def log(line):
        job["text"] = str(line)
        job["log"].append(job["text"])

    def on_progress(done, total):
        job["progress"] = [int(done), int(total)]
        job["text"] = "封面 %d/%d" % (done, total)

    def on_stage(event):
        job["stage"] = dict(event)
        if event.get("phase") == "merge" and event.get("status") == "completed":
            job["catalog_updated"] = True
            job.setdefault("committed_catalogues", {})[event.get("catalog_id")] = dict(event)

    try:
        results = []
        targets = ["western_slg", "dlsite"] if job.get("catalog_id") == "all" else [job.get("catalog_id", "western_slg")]
        for target in targets:
            if job["stop"].is_set():
                job["cancelled"] = True
                break
            try:
                with slg_db.session() as conn:
                    result = slg_sync_server.pull(conn, log=log, should_stop=job["stop"].is_set,
                        on_progress=on_progress, catalog_id=target, download_images=False, on_stage=on_stage)
                if target == "dlsite":
                    result["covers_available"] = result.get("covers", 0)
                    result["covers"] = 0
                result["catalog_id"] = target
                results.append(result)
                job["catalog_updated"] |= bool(result.get("catalog_updated", not result.get("unchanged", True)))
                if result.get("cancelled"):
                    job["cancelled"] = True
                    break
            except __import__("slg_sync_progress").SyncCancelled:
                job["cancelled"] = True
                results.append({"catalog_id": target, "cancelled": True,
                                "catalog_updated": target in job.get("committed_catalogues", {})})
                break
            except Exception as exc:
                results.append({"catalog_id": target, "error": str(exc)})
        job["summary"] = {"catalogues": results, "new": sum(x.get("new", 0) for x in results),
            "covers": sum(x.get("covers", 0) for x in results),
            "catalogue": sum(x.get("catalogue", 0) for x in results),
            "unchanged": bool(results) and all(x.get("unchanged", False) for x in results)}
        failures = [x for x in results if x.get("error")]
        if failures:
            job["error"] = "；".join(x["catalog_id"] + ": " + x["error"] for x in failures)
        with _COVER_LOCK:
            for cache in _COVER_CACHES.values():
                cache.clear_retry()
            _INDEX_RETRIES.clear()
        job["text"] = ("目录已更新，封面下载已停止" if job["catalog_updated"]
                       else "已停止，目录未变更") if job["cancelled"] else "完成"
        if failures and not job["cancelled"]:
            job["text"] = "部分失败" if len(failures) < len(results) else "失败"
    except Exception as exc:  # noqa: BLE001 - 桥约定错误走结果字段
        job["error"] = "%s: %s" % (type(exc).__name__, exc)
        job["text"] = "失败"
    finally:
        job["running"] = False


def _h_sync_start(req):
    from slg_catalogues import normalize_catalogue
    catalog_id = normalize_catalogue(req.get("catalog_id", "western_slg"))
    with _JOBS_LOCK:
        current = _JOBS.get(_SYNC_JOB["id"])
        if current and current["running"]:
            return _job_snapshot(current)
        job = {"id": "sync-%d" % len(_JOBS), "running": True, "text": "开始同步…",
               "progress": None, "summary": None, "error": None, "log": [],
               "stop": threading.Event(), "cancel_requested": False,
               "cancelled": False, "catalog_updated": False}
        job["catalog_id"] = catalog_id
        _JOBS[job["id"]] = job
        _SYNC_JOB["id"] = job["id"]
    threading.Thread(target=_sync_worker, args=(job,), daemon=True).start()
    return _job_snapshot(job)


def _h_sync_poll(req):
    with _JOBS_LOCK:
        job = _JOBS.get(str(req.get("job") or _SYNC_JOB["id"]))
    if job is None:
        return _err("没有该同步任务")
    return _job_snapshot(job)


def _h_sync_cancel(req):
    with _JOBS_LOCK:
        job = _JOBS.get(str(req.get("job") or _SYNC_JOB["id"]))
    if job is None:
        return _err("没有该同步任务")
    with _JOBS_LOCK:
        if not job["running"]:
            return {"requested": False}
        job["cancel_requested"] = True
        job["stop"].set()
    return {"requested": True}


# --- 账号 ------------------------------------------------------------------


def _account_module():
    _boot()
    import slg_account
    return slg_account


def _strip_key(value):
    """登录密钥/恢复码粘贴清洗：去除所有空白字符。"""
    return "".join(str(value or "").split())


def _h_account_status(req):
    slg_account = _account_module()
    try:
        current = slg_account.session()
    except Exception:  # noqa: BLE001 - 未登录/凭证损坏都视为未登录态
        current = None
    return {"logged_in": bool(current),
            "account_id": (current or {}).get("account_id"),
            "session_epoch": _SESSION_EPOCH,
            "developer_mode": bool(slg_account._DEVELOPER_MODE)}


def _h_account_create(req):
    slg_account = _account_module()
    result = slg_account.create()
    result.pop("device_token", None)
    result.pop("legacy_migration", None)
    return {"account": result}


def _h_account_login(req):
    slg_account = _account_module()
    key = _strip_key(req.get("login_key"))
    if not key:
        return _err("请输入登录密钥")
    code = _strip_key(req.get("recovery_code")) or None
    result = slg_account.login(key, code)
    result.pop("device_token", None)
    result.pop("legacy_migration", None)
    return {"account": result}


def _h_account_logout(req):
    global _SESSION_EPOCH
    slg_account = _account_module()
    slg_account.forget_session()
    _SESSION_EPOCH += 1
    return {"logged_in": False}


def _h_account_me(req):
    import slg_titles
    slg_account = _account_module()
    result = slg_account.me()
    result.pop("legacy_migration", None)
    title = slg_titles.title_by_id(result.get("equipped_title") or slg_titles.DEFAULT_TITLE_ID)
    result["title_name"] = (title or {}).get("name", "普通用户")
    result["appearance_names"] = {
        slot: (slg_titles.shop_item_by_id(tid) or {}).get("name", tid)
        for slot, tid in (result.get("equipped_appearances") or {}).items() if tid
    }
    return {"me": result}


def _h_account_devices(req):
    slg_account = _account_module()
    return {"devices": slg_account.devices().get("devices", [])}


def _h_account_revoke_device(req):
    slg_account = _account_module()
    slg_account.revoke_device(int(req.get("id", 0)))
    return {}


def _h_account_rotate(req):
    slg_account = _account_module()
    key = _strip_key(req.get("login_key"))
    if not key:
        return _err("请输入登录密钥")
    result = slg_account.rotate(key, _strip_key(req.get("recovery_code")) or None)
    result.pop("device_token", None)
    return {"account": result}


def _h_account_update_profile(req):
    slg_account = _account_module()
    nickname = req.get("nickname")
    message = req.get("profile_message")
    if nickname is None and message is None:
        return _err("没有要修改的字段")
    result = slg_account.update_profile(
        nickname=str(nickname) if nickname is not None else None,
        profile_message=str(message) if message is not None else None)
    return {"me": result}


# --- 评论 ------------------------------------------------------------------


def _h_comments_page(req):
    import slg_comments
    _boot()
    slug = str(req.get("slug") or "")
    if not slug:
        return _err("缺少 slug")
    local = [dict(r) for r in _conn().execute(
        "SELECT id, content, created_at FROM comments WHERE game_slug=?"
        " AND cloud_id IS NULL ORDER BY id DESC", (slug,))]
    page = slg_comments.fetch_comments_page(
        slug, page=int(req.get("page", 1)), limit=int(req.get("limit", 20)),
        sort=str(req.get("sort") or "latest"))
    if page is None:
        # 离线/服务不可达：仍返回本地私人评论与离线标记，UI 给重试
        return {"offline": True, "comments": [], "local_private": local,
                "page": int(req.get("page", 1)), "total_count": 0, "total_pages": 0}
    page["local_private"] = local
    import urllib.parse
    account = _account_module()
    aid = _h_account_status({}).get("account_id")
    page["account_id"] = aid
    own_ids = set()
    if aid:
        try:
            mine = account.authenticated("/account/comments?" + urllib.parse.urlencode({"game": slug}))
            own_ids = {str(row["id"]) for row in mine.get("comments", [])
                       if isinstance(row, dict) and row.get("id") is not None}
        except Exception:
            # Public routes deliberately omit account IDs. A failed private
            # lookup must not grant ownership or stop anonymous comment reads.
            page["ownership_unavailable"] = True
    for row in page.get("comments", []):
        row["own"] = bool(aid and (str(row.get("id")) in own_ids or
                           str(row.get("account_id")) == str(aid)))
    return page


def _h_comments_post(req):
    import slg_comments
    _boot()
    slug = str(req.get("slug") or "")
    content = str(req.get("content") or "").strip()
    if not content:
        return _err("评论内容不能为空")
    if bool(req.get("public")):
        result = slg_comments.submit_comment(slug, content)
        return {"submitted": result}
    import slg_db
    comment_id = slg_db.add_comment(_conn(), slug, content)
    return {"local_id": comment_id}


def _h_comments_mine(req):
    slg_account = _account_module()
    return {"comments": slg_account.authenticated("/account/comments").get("comments", [])}


def _h_comments_local_delete(req):
    import slg_db
    slg_db.delete_comment(_conn(), int(req.get("id", 0)))
    return {}


def _cloud_comment_id(req):
    value = req.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("云端评论 ID 必须是非空字符串")
    return value


def _h_comments_cloud_delete(req):
    import slg_comments
    ok = slg_comments.delete_comment(_cloud_comment_id(req))
    if not ok:
        return _err("删除云端评论失败")
    return {}


def _h_comments_vote(req):
    import slg_comments
    value = int(req.get("value", 0))
    if value not in (-1, 0, 1):
        return _err("value 必须是 -1/0/1")
    return {"vote": slg_comments.vote_comment(_cloud_comment_id(req), value)}


def _h_comments_report(req):
    import slg_comments
    if not slg_comments.report_comment(_cloud_comment_id(req)):
        return _err("举报提交失败")
    return {}


# --- 游戏化（云端账号路径 + 本地目录）---------------------------------------

_SHOP_CATALOG_FIELDS = ("id", "name", "cost", "kind", "rarity", "detail")


def _h_points_signin(req):
    slg_account = _account_module()
    return {"signin": slg_account.signin()}


def _h_points_ledger(req):
    import slg_db
    conn = _conn()
    trusted = slg_db._trusted(conn, "points_log")
    rows = conn.execute(
        "SELECT id, at, delta, reason FROM points_log WHERE %s"
        " ORDER BY id DESC LIMIT ? OFFSET ?" % trusted,
        (int(req.get("limit", 50)), (max(1, int(req.get("page", 1))) - 1) * 50)
    ).fetchall()
    return {"ledger": [dict(r) for r in rows],
            "source": "local",
            "balance": slg_db.points_balance(conn),
            "earned_spent": slg_db.points_flow(conn)}


def _h_shop_list(req):
    import slg_titles
    items = []
    for item in slg_titles.available_shop_items():
        if not isinstance(item, dict):
            continue
        items.append({k: item.get(k) for k in _SHOP_CATALOG_FIELDS if k in item}
                     | {k: v for k, v in item.items()
                        if k not in _SHOP_CATALOG_FIELDS})
    return {"items": items}


def _h_shop_buy(req):
    slg_account = _account_module()
    return {"buy": slg_account.buy(str(req.get("item_id", "")))}


def _h_redeem(req):
    slg_account = _account_module()
    code = str(req.get("code") or "").strip()
    if not code:
        return _err("请输入兑换码")
    return {"redeem": slg_account.redeem_group(code)}


def _h_quests(req):
    slg_account = _account_module()
    return {"quests": slg_account.get_quests()}


def _h_quest_claim(req):
    slg_account = _account_module()
    return {"claim": slg_account.claim_quest(str(req.get("quest_id", "")))}


def _h_wardrobe_list(req):
    """衣橱：云端已拥有（me）+ 本地目录定义合并。"""
    import slg_titles
    slg_account = _account_module()
    me = slg_account.me()
    owned = list(me.get("titles") or [])
    equipped = me.get("equipped_title")
    equipped_cosmetic = me.get("equipped_cosmetic") or ""
    appearances = me.get("equipped_appearances") or {}
    titles = []
    for tid in dict.fromkeys(owned + [slg_titles.DEFAULT_TITLE_ID]):
        t = slg_titles.title_by_id(tid)
        if t:
            titles.append({"id": tid, "name": t.get("name") or tid,
                           "rarity": t.get("rarity") or "普通",
                           "equipped": tid == equipped})
    decorations = []
    for tid in dict.fromkeys(owned):
        item = slg_titles.shop_item_by_id(tid)
        if item and item.get("kind") == "decoration":
            slot = item.get("appearance") or "comment_frame"
            active = appearances.get(slot) or (equipped_cosmetic if slot == "comment_frame" else "")
            decorations.append({"id": tid, "name": item.get("name") or tid,
                                "rarity": item.get("rarity") or "普通",
                                "slot": slot, "equipped": tid == active})
    return {"titles": titles, "appearances": decorations, "cosmetics": [],
            "source": "cloud", "equipped": equipped,
            "equipped_cosmetic": equipped_cosmetic,
            "equipped_appearances": appearances,
            "owned_count": len(owned)}


def _h_wardrobe_equip(req):
    slg_account = _account_module()
    title_id = req.get("title_id")
    if title_id:
        return {"equip": slg_account.equip_title(str(title_id))}
    cosmetic = req.get("cosmetic_id")
    if cosmetic is not None:
        return {"equip": slg_account.equip_cosmetic(str(cosmetic))}
    slot = req.get("appearance_slot")
    if slot:
        return {"equip": slg_account.equip_appearance(
            str(slot), str(req.get("item_id", "")))}
    return _err("缺少装备目标")


def _h_leaderboard(req):
    slg_account = _account_module()
    board = str(req.get("board") or "points")
    return {"board": slg_account.leaderboard(board, int(req.get("limit", 30)))}


def _h_lottery_status(req):
    import slg_titles
    account = _account_module()
    if account.session():
        me = account.me()
        history = [dict(row, text=slg_titles.lottery_prize_text(row))
                   for row in (me.get("recent_lottery_history") or [])[:10]]
        return {"paytable": slg_titles.lottery_paytable(),
                "draws_today": me.get("draws_today", 0),
                "history": history,
                "balance": me.get("balance", 0), "source": "cloud"}
    return {"paytable": slg_titles.lottery_paytable(),
            "draws_today": slg_titles.lottery_draws_today(_conn()),
            "history": slg_titles.lottery_history(_conn())[:10],
            "source": "local"}


def _h_lottery_spin(req):
    import uuid
    import slg_titles

    slg_account = _account_module()
    draw = slg_account.lottery_draw(str(req.get("request_id") or uuid.uuid4().hex))
    if isinstance(draw.get("prize"), dict):
        draw = dict(draw, prize=dict(draw["prize"], text=slg_titles.lottery_prize_text(draw["prize"])))
    return {"draw": draw}


# --- 工具 ------------------------------------------------------------------


_TRANSLATE_LOCK = threading.RLock()
_TRANSLATE_RUNNING = False


def _h_translate_settings(req):
    import slg_db
    import slg_engines as e
    conn = _conn()
    keys = {"base_url": e.PREF_BASE_URL, "model": e.PREF_MODEL,
            "engine": e.PREF_ENGINE, "provider": e.PREF_PROVIDER}
    out = {name: slg_db.get_pref(conn, key) for name, key in keys.items()}
    out["has_key"] = bool(slg_db.get_pref(conn, e.PREF_KEY))
    out["presets"] = e.translation_presets()
    # Resolve old installations on read only; never replace their model.
    config = e.resolve_config(conn)
    for name in keys:
        if not out[name]:
            out[name] = getattr(config, name)
    if not any(slg_db.get_pref(conn, key) is not None for key in
               (e.PREF_MODEL, e.PREF_PROVIDER, e.PREF_BASE_URL, e.PREF_KEY)):
        out.update(provider="deepseek", base_url=e.DEFAULT_BASE_URL,
                   model="deepseek-flash")
    with _TRANSLATE_LOCK:
        out["running"] = _TRANSLATE_RUNNING or any(j.get("running") and j["id"].startswith("detail-translate") for j in _JOBS.values())
    return out


def _h_translate_save(req):
    import urllib.parse
    import slg_db
    import slg_engines as e
    with _TRANSLATE_LOCK:
        if _h_translate_settings({})["running"]:
            return _err("翻译任务进行中，请稍后保存")
        conn = _conn()
        old = _h_translate_settings({})
        fields = {name: str(req.get(name, old[name]) or "").strip()
                  for name in ("provider", "base_url", "model", "engine")}
        if fields["provider"] not in e.PROVIDER_BY_ID:
            return _err("请选择有效服务商")
        if fields["engine"] not in (e.ENGINE_OPENAI, e.ENGINE_FREE):
            return _err("请选择有效翻译方式")
        parsed = urllib.parse.urlsplit(fields["base_url"])
        if fields["engine"] == e.ENGINE_OPENAI:
            if (parsed.scheme not in ("https", "http") or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                return _err("接口地址需为完整 HTTP(S) 地址，不能含凭据、查询或片段")
            if not fields["model"]:
                return _err("请填写模型名称")
        same_target = (fields["provider"] == old["provider"] and
                       fields["base_url"].rstrip("/") == str(old["base_url"]).rstrip("/"))
        key = str(req.get("api_key") or "").strip()
        if fields["engine"] == e.ENGINE_OPENAI and not key and not (same_target and old["has_key"]):
            return _err("请为当前服务商和接口填写 API Key")
        mapping = {"provider": e.PREF_PROVIDER, "base_url": e.PREF_BASE_URL,
                   "model": e.PREF_MODEL, "engine": e.PREF_ENGINE}
        # One transaction: no set_pref calls (each would commit separately).
        with conn:
            for name, pref in mapping.items():
                conn.execute("INSERT OR REPLACE INTO prefs(key,value) VALUES (?,?)",
                             (pref, fields[name]))
            if key or not same_target:
                conn.execute("INSERT OR REPLACE INTO prefs(key,value) VALUES (?,?)",
                             (e.PREF_KEY, key))
        return _h_translate_settings({})


def _h_translate_tags_run(req):
    """标签批量翻译后台 job（进度轮询）。"""
    import slg_translate
    global _TRANSLATE_RUNNING
    with _TRANSLATE_LOCK:
        if _TRANSLATE_RUNNING:
            return _err("已有翻译任务进行中")
        _TRANSLATE_RUNNING = True

    def worker(job):
        global _TRANSLATE_RUNNING
        try:
            def log(line):
                job["text"] = str(line)
                job["log"].append(job["text"])

            count = slg_translate.run_tag_translation(
                _conn(), api_key=str(req.get("api_key") or "") or None,
                model=str(req.get("model") or "") or None,
                limit=int(req.get("limit", 0) or 0), log=log)
            job["summary"] = {"translated": count}
        except Exception as exc:  # noqa: BLE001
            job["error"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            job["running"] = False
            with _TRANSLATE_LOCK:
                _TRANSLATE_RUNNING = False

    try:
        return _start_generic_job("translate", worker)
    except Exception:
        with _TRANSLATE_LOCK:
            _TRANSLATE_RUNNING = False
        raise


def _start_generic_job(kind, worker):
    with _JOBS_LOCK:
        job = {"id": "%s-%d" % (kind, len(_JOBS)), "running": True,
               "text": "准备中…", "progress": None, "summary": None,
               "error": None, "log": [], "stop": threading.Event()}
        _JOBS[job["id"]] = job
    threading.Thread(target=worker, args=(job,), daemon=True).start()
    return _job_snapshot(job)


def _h_job_poll(req):
    with _JOBS_LOCK:
        job = _JOBS.get(str(req.get("job") or ""))
    if job is None:
        return _err("没有该任务")
    return _job_snapshot(job)


def _h_diagnose(req):
    slg_account = _account_module()
    return {"diagnostics": slg_account.diagnose_connectivity()}


def _h_remote_config(req):
    import slg_remote
    config = slg_remote.fetch_config()
    return {"config": config, "offline": not bool(config)}


def _h_backup_export(req):
    import slg_db
    return {"data": slg_db.export_user_data(_conn())}


def _read_backup(req):
    import slg_db
    path = str(req.get("path") or "")
    if not path or not os.path.exists(path):
        raise ValueError("备份文件不存在")
    with open(path, "r", encoding="utf-8-sig") as fh:
        payload = json.load(fh)
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    return data, slg_db.validate_user_data(data)


def _h_backup_preview(req):
    _, preview = _read_backup(req)
    return {"preview": preview}


def _h_backup_import(req):
    import slg_db
    data, preview = _read_backup(req)
    conn = _conn()
    slg_db.import_user_data(conn, data)
    conn.commit()
    return {"imported": preview["counts"]}


def _h_update_check(req):
    import slg_update
    release = slg_update.check(_conn(),
                               str(req.get("local_version") or "0"),
                               force=bool(req.get("force")),
                               want=".apk", android=True)
    return {"release": release}


# --- 想玩版本提醒 / 反馈 / 邀请 / 维护补偿 / 遥测 ----------------------------


def _h_wishlist_updates(req):
    import slg_db
    _boot()
    return {"changes": [dict(r) for r in
                        slg_db.wishlist_version_changes(_conn())]}


def _h_wishlist_mark_seen(req):
    import slg_db
    marked = slg_db.mark_wishlist_version_seen(
        _conn(), int(req.get("id", 0)), req.get("version"))
    return {"marked": bool(marked)}


def _h_feedback_submit(req):
    slg_account = _account_module()
    category = str(req.get("category") or "")
    if category not in ("bug", "suggestion", "data_correction", "other"):
        return _err("反馈类型无效")
    content = str(req.get("content") or "").strip()
    if not 20 <= len(content) <= 500:
        return _err("反馈内容需要填写 20–500 个字")
    return {"feedback": slg_account.submit_feedback(category, content)}


def _h_referral_me(req):
    slg_account = _account_module()
    return {"referral": slg_account.referrals_me()}


def _h_referral_redeem(req):
    slg_account = _account_module()
    code = "".join(str(req.get("code") or "").split())
    if not code:
        return _err("请输入邀请码")
    return {"redeem": slg_account.redeem_referral(code)}


def _h_maintenance_reward_status(req):
    slg_account = _account_module()
    return {"status": slg_account.maintenance_reward_status()}


def _h_maintenance_reward_claim(req):
    slg_account = _account_module()
    campaign_id = str(req.get("campaign_id") or "").strip()
    if not campaign_id:
        return _err("缺少补偿活动编号")
    return {"claim": slg_account.claim_maintenance_reward(campaign_id)}


def _h_telemetry_report(req):
    """fire-and-forget：slg_remote.report 自带守护线程与吞错语义。"""
    import slg_remote
    _boot()
    data = req.get("data")
    slg_remote.report(_conn(), str(req.get("event") or "app"),
                      dict(data) if isinstance(data, dict) else {})
    return {}


_HANDLERS = {
    "dlsite.filters": _h_dlsite_filters,
    "cover.get": _h_cover_get,
    "ping": _h_ping,
    "stats": _h_stats,
    "catalog.list": _h_catalog_list,
    "game.get": _h_game_get,
    "game.rate": _h_game_rate,
    "game.state": _h_game_state,
    "game.note": _h_game_note,
    "collections.list": _h_collections_list,
    "collection.save": _h_collection_save,
    "collection.delete": _h_collection_delete,
    "game.collections.set": _h_game_collections_set,
    "tags.list": _h_tags_list,
    "usergames.add": _h_usergames_add,
    "usergames.update": _h_usergames_update,
    "usergames.delete": _h_usergames_delete,
    "sync.start": _h_sync_start,
    "sync.poll": _h_sync_poll,
    "sync.cancel": _h_sync_cancel,
    "account.status": _h_account_status,
    "account.create": _h_account_create,
    "account.login": _h_account_login,
    "account.logout": _h_account_logout,
    "account.me": _h_account_me,
    "account.devices": _h_account_devices,
    "account.revoke_device": _h_account_revoke_device,
    "account.rotate": _h_account_rotate,
    "account.update_profile": _h_account_update_profile,
    "comments.page": _h_comments_page,
    "comments.post": _h_comments_post,
    "comments.mine": _h_comments_mine,
    "comments.local_delete": _h_comments_local_delete,
    "comments.cloud_delete": _h_comments_cloud_delete,
    "comments.vote": _h_comments_vote,
    "comments.report": _h_comments_report,
    "points.signin": _h_points_signin,
    "points.ledger": _h_points_ledger,
    "shop.list": _h_shop_list,
    "shop.buy": _h_shop_buy,
    "redeem.code": _h_redeem,
    "quests.list": _h_quests,
    "quests.claim": _h_quest_claim,
    "wardrobe.list": _h_wardrobe_list,
    "wardrobe.equip": _h_wardrobe_equip,
    "leaderboard.list": _h_leaderboard,
    "lottery.status": _h_lottery_status,
    "lottery.spin": _h_lottery_spin,
    "translate.settings": _h_translate_settings,
    "translate.save": _h_translate_save,
    "translate.tags.run": _h_translate_tags_run,
    "job.poll": _h_job_poll,
    "diagnose.run": _h_diagnose,
    "remote.config": _h_remote_config,
    "backup.export": _h_backup_export,
    "backup.import": _h_backup_import,
    "backup.preview": _h_backup_preview,
    "update.check": _h_update_check,
    "wishlist.updates": _h_wishlist_updates,
    "wishlist.mark.seen": _h_wishlist_mark_seen,
    "feedback.submit": _h_feedback_submit,
    "referral.me": _h_referral_me,
    "referral.redeem": _h_referral_redeem,
    "maintenance_reward.status": _h_maintenance_reward_status,
    "maintenance_reward.claim": _h_maintenance_reward_claim,
    "telemetry.report": _h_telemetry_report,
}


def call(payload):
    """桥入口：JSON 字符串进、JSON 字符串出；任何异常都转为 ok:false。"""
    try:
        req = json.loads(payload) if isinstance(payload, str) else dict(payload)
        if not isinstance(req, dict):
            raise ValueError("请求必须是 JSON 对象")
        method = str(req.get("method") or "")
        handler = _HANDLERS.get(method)
        if handler is None:
            result = _err("未知方法: %s" % method)
        else:
            from contextlib import nullcontext
            auth = method in ("account.login", "account.create", "account.rotate")
            network = method in _NETWORK_METHODS
            snapshot, epoch = None, None
            if network or auth:
                with _INIT_LOCK:
                    account = _account_module()
                    snapshot = account.session()
                    epoch = _SESSION_EPOCH
            with (_AUTH_LOCK if auth else nullcontext()):
              with (nullcontext() if network or auth or method == "cover.get" else _INIT_LOCK):
                try:
                    if auth:
                        _TLS.auth_epoch = epoch
                    import slg_account as session_api
                    with (session_api.use_session_snapshot(snapshot) if network or auth else nullcontext()):
                        result = handler(req) or {}
                    if network:
                        with _INIT_LOCK:
                            if epoch != _SESSION_EPOCH:
                                result = _err("账号身份已改变，请重新打开此功能")
                    conn = getattr(_TLS, "conn", None)
                    if conn is not None and conn.in_transaction:
                        if isinstance(result, dict) and result.get("ok") is False:
                            conn.rollback()
                        else:
                            conn.commit()
                except Exception:
                    conn = getattr(_TLS, "conn", None)
                    if conn is not None and conn.in_transaction:
                        conn.rollback()
                    raise
                finally:
                    if auth and hasattr(_TLS, "auth_epoch"):
                        del _TLS.auth_epoch
    except Exception as exc:  # noqa: BLE001 - 桥约定错误走结果字段
        result = _err("%s: %s" % (type(exc).__name__, exc))
    if not isinstance(result, dict):
        result = {"value": result}
    result.setdefault("ok", True)
    return json.dumps(result, ensure_ascii=False)


# HTTP runs outside the initialization/database lock, pinned to its queued identity.
_NETWORK_METHODS = {
    'account.profile', 'account.public_profile', 'account.me', 'account.update_profile',
    'account.devices', 'account.revoke_device', 'comments.page', 'comments.post',
    'comments.mine', 'comments.cloud_delete', 'comments.vote', 'comments.report',
    'points.signin', 'shop.list', 'shop.buy', 'redeem.code', 'quests.list', 'quests.claim',
    'wardrobe.list', 'wardrobe.equip', 'leaderboard.list', 'lottery.status', 'lottery.spin',
    'diagnose.run', 'remote.config', 'update.check', 'feedback.submit', 'referral.me',
    'referral.redeem', 'maintenance_reward.status', 'maintenance_reward.claim',
    'replies.page', 'replies.mine', 'replies.post', 'replies.delete', 'replies.report',
    'replies.context', 'notifications.list', 'notifications.read',
}

import sys as _sys
import slg_android_029 as _android_029
_android_029.install(_sys.modules[__name__])
