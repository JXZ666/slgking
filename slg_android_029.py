"""Android 0.29 additions; shared desktop core remains UI-independent."""
import threading
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

_TRANSLATIONS = {}
_TRANSLATION_LOCK = threading.RLock()
_FIELD_FUTURES = {}
_FIELD_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='detail-field')


def translation(conn, kind, row):
    import slg_db as db
    cached = db.get_translation_row(conn, kind, row['id'], row[kind])
    if cached is None:
        return None, 'missing'
    text, engine = cached
    state = ('manual' if engine == db.ENGINE_MANUAL else
             'untranslatable' if engine == db.ENGINE_UNTRANSLATED else 'auto')
    return text, state


def install(api):
    import slg_account as account
    import slg_db as db
    import slg_titles
    import slg_comment_replies as replies

    def profile(req):
        value = dict(account.authenticated('/account/me'))
        for name in ('legacy_migration', 'device_token', 'login_key', 'recovery_code'):
            value.pop(name, None)
        title = slg_titles.title_by_id(value.get('equipped_title') or slg_titles.DEFAULT_TITLE_ID)
        value['title_name'] = (title or {}).get('name', '普通用户')
        value['appearance_names'] = {slot: (slg_titles.shop_item_by_id(tid) or {}).get('name', tid)
                                     for slot, tid in (value.get('equipped_appearances') or {}).items() if tid}
        return {'me': value}

    def public_profile(req):
        response = account.public_profile(req.get('public_id'))
        row = response.get('profile')
        if not isinstance(row, dict) or row.get('public_id') != req.get('public_id'):
            raise ValueError('公开资料返回不完整')
        safe = {k: row.get(k) for k in ('public_id', 'nickname', 'profile_visible')}
        safe['profile_visible'] = row.get('profile_visible') is True
        if safe['profile_visible']:
            safe.update({k: row.get(k) for k in ('equipped_title', 'equipped_appearances', 'profile_message')})
            title_id = row.get('equipped_title')
            title = slg_titles.title_by_id(title_id) if isinstance(title_id, str) else None
            safe['title_name'] = (title or {}).get('name')
            appearances = row.get('equipped_appearances')
            safe['appearance_names'] = {slot: item['name'] for slot, tid in (appearances.items() if isinstance(appearances, dict) else [])
                                        if isinstance(tid, str) and (item := slg_titles.shop_item_by_id(tid)) and item.get('name')}
        return {'profile': safe}

    def update_profile(req):
        return {'me': account.update_profile(nickname=req.get('nickname'),
                profile_message=req.get('profile_message'), profile_visible=req.get('profile_visible'))}

    original_get = api._h_game_get

    def game_get(req):
        result = original_get(req)
        if result.get('ok') is False:
            return result
        item = result['game']
        conn = api._conn()
        row = db.get_game(conn, item['id'])
        item['overview_zh'], item['overview_translation_state'] = translation(conn, 'overview', row)
        _, item['title_translation_state'] = translation(conn, 'title', row)
        import slg_hidden_games
        item['hidden'] = slg_hidden_games.is_hidden(conn, item['id'])
        return result

    def translation_save(req):
        conn = api._conn()
        gid, kind = int(req['id']), req.get('kind')
        if kind not in ('title', 'overview') or db.get_game(conn, gid) is None:
            raise ValueError('无效的游戏或译文字段')
        text = req.get('text')
        if not isinstance(text, str):
            raise ValueError('译文必须是文字')
        if text.strip():
            db.set_manual_translation(conn, kind, gid, text.strip())
        else:
            db.delete_manual_translation(conn, kind, gid)
        return game_get({'id': gid})

    def tags_set(req):
        gid, conn = int(req['id']), api._conn()
        tags = req.get('tags')
        if db.get_game(conn, gid) is None or not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise ValueError('无效的游戏或标签')
        tags = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
        if len(tags) > 500 or any(len(t) > 200 for t in tags):
            raise ValueError('标签过多或过长')
        db.set_tags(conn, gid, tags, clear=True)
        db.recompute_weights(conn)
        return game_get({'id': gid})

    def hidden_set(req):
        import slg_hidden_games
        slg_hidden_games.set_hidden(api._conn(), int(req['id']), req.get('hidden'))
        return {}

    def catalogue(req):
        from slg_catalogue_query import query_catalogue_page
        from slg_catalogues import normalize_catalogue
        view = req.get('view', 'all')
        catalog = normalize_catalogue(req.get('catalog_id', 'all' if view in ('user', 'collection') else 'western_slg'))
        sort = req.get('sort', 'score')
        if catalog == 'dlsite' and sort in ('score', 'rating', 'heat'):
            sort = 'source_rating'
        elif catalog == 'all' and sort in ('score', 'rating', 'heat'):
            sort = 'title'
        spec = dict(catalog_id=catalog, origin='main', sort=sort, desc=bool(req.get('desc', True)),
                    search=req.get('q'), include=req.get('tags', []), exclude=req.get('exclude', []),
                    dlsite_languages=req.get('languages', []), dlsite_work_types=req.get('work_types', []),
                    date_preset=req.get('date_preset', 'all'), date_start=req.get('date_start'),
                    date_end=req.get('date_end'), hidden_mode=req.get('hidden_mode', 'exclude'),
                    statuses=req.get('statuses', []))
        if view == 'user':
            spec.update(origin='user', category_ids=req.get('category_ids', []), uncategorized=bool(req.get('uncategorized')))
        elif view == 'collection':
            spec.update(origin=None, collection_id=int(req.get('collection_id', 0)))
        elif view in ('want', 'downloaded'):
            spec['statuses'] = [view]
        if spec['hidden_mode'] == 'only':
            spec['origin'] = None
        page, size, _ = api._page(req)
        conn = api._conn()
        result = query_catalogue_page(conn, spec, page, size)
        titles = api._title_zh_map(conn)
        return {'items': [api._item(row, titles) for row in result.rows],
                'total': result.total, 'page': result.page, 'page_size': size}

    def start_translate(req):
        import slg_engines
        import slg_translate
        conn = api._conn()
        gid = int(req['id'])
        row = db.get_game(conn, gid)
        fields = req.get('fields', ['title', 'overview'])
        if row is None or not isinstance(fields, list) or not fields or any(f not in ('title', 'overview') for f in fields):
            raise ValueError('无效的游戏或翻译字段')
        fields = list(dict.fromkeys(fields))
        config = slg_engines.resolve_config(conn)
        credential_id = hashlib.sha256((config.api_key or '').encode('utf-8')).digest()
        key = (gid, tuple(fields), tuple(db.src_hash(row[f]) for f in fields),
               config.engine, config.provider, config.base_url, config.model, credential_id)
        with _TRANSLATION_LOCK:
            previous = _TRANSLATIONS.get(key)
            if previous and api._JOBS.get(previous, {}).get('running'):
                return api._job_snapshot(api._JOBS[previous])

            def worker(job):
                results = {}
                job['summary'] = {'fields': results}

                def one(kind):
                    c = db.connect()
                    try:
                        source = row[kind]
                        if not source:
                            return {'status': 'skipped'}
                        cached, state = translation(c, kind, row)
                        if state != 'missing':
                            return {'status': 'cached', 'text': cached}
                        engine = config.build()
                        if engine.needs_key and not config.api_key:
                            return {'status': 'error', 'error': '尚未配置 API Key，请打开翻译设置'}
                        fn = slg_translate.translate_title if kind == 'title' else slg_translate.translate_overview
                        try:
                            text = fn(source, config.api_key, config.model, engine=engine)
                            marker = config.model
                        except slg_translate.TitleGuardError:
                            text, marker = source, db.ENGINE_UNTRANSLATED
                        if not isinstance(text, str) or not text.strip():
                            raise ValueError('空译文')
                        with api._INIT_LOCK:
                            current = db.get_game(c, gid)
                            if current is None or current[kind] != source:
                                return {'status': 'skipped'}
                            db.set_auto_translation(c, kind, gid, source, text, engine=marker)
                            actual, _ = translation(c, kind, current)
                        return {'status': 'succeeded', 'text': actual}
                    except Exception:
                        # Provider exceptions can contain URLs/keys: do not return them.
                        return {'status': 'error', 'error': '翻译失败，请检查设置或网络后重试'}
                    finally:
                        c.close()

                try:
                    pending = {}
                    with _TRANSLATION_LOCK:
                        for kind in fields:
                            field_key = (gid, kind, db.src_hash(row[kind]), config.engine,
                                         config.provider, config.base_url, config.model, credential_id)
                            future = _FIELD_FUTURES.get(field_key)
                            if future is None or future.done():
                                future = _FIELD_POOL.submit(one, kind)
                                _FIELD_FUTURES[field_key] = future
                            pending[kind] = (field_key, future)
                    names = {future: kind for kind, (_, future) in pending.items()}
                    for future in as_completed(names):
                        results[names[future]] = future.result()
                    job['text'] = '翻译完成' if not any(v['status'] == 'error' for v in results.values()) else '部分翻译失败'
                finally:
                    job['running'] = False
                    with _TRANSLATION_LOCK:
                        for field_key, future in locals().get('pending', {}).values():
                            if future.done() and _FIELD_FUTURES.get(field_key) is future:
                                _FIELD_FUTURES.pop(field_key, None)

            snapshot = api._start_generic_job('detail-translate', worker)
            _TRANSLATIONS[key] = snapshot['job']
            return snapshot

    api._HANDLERS.update({
        'account.profile': profile, 'account.public_profile': public_profile,
        'account.update_profile': update_profile, 'catalog.list': catalogue, 'game.get': game_get,
        'game.translate.start': start_translate, 'game.translation.save': translation_save,
        'game.tags.set': tags_set, 'game.hidden.set': hidden_set,
        'replies.page': lambda r: replies.fetch_replies(r['root_id'], r.get('page', 1), r.get('focus_id')),
        'replies.mine': lambda r: replies.fetch_my_replies(r['root_id'], r.get('page', 1)),
        'replies.post': lambda r: replies.submit_reply(r['root_id'], r.get('content'), r.get('reply_to_id'), r.get('operation_id')),
        'replies.delete': lambda r: replies.delete_reply(r['id']),
        'replies.report': lambda r: replies.report_reply(r['id']),
        'replies.context': lambda r: replies.fetch_context(r['id']),
        'notifications.list': lambda r: replies.fetch_notifications(r.get('page', 1)),
        'notifications.read': lambda r: replies.mark_notifications_read(r.get('ids'), r.get('all', False)),
    })

    # Only protect credential persistence, never wait for an HTTP request here.
    original_save = account.save_session
    if not getattr(original_save, '_android_epoch_guard', False):
        def save_session(aid, token):
            with api._INIT_LOCK:
                expected = getattr(api._TLS, 'auth_epoch', None)
                if expected is not None and expected != api._SESSION_EPOCH:
                    raise account.AccountError('账号操作已取消，设备身份已改变')
                original_save(aid, token)
                api._SESSION_EPOCH += 1
        save_session._android_epoch_guard = True
        account.save_session = save_session
