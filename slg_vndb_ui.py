"""VNDB public catalogue UI integration; same-server sync, no upstream query."""
import time
import slg_db
import slg_vndb_online as online

class VNDBCatalogueUI:
    def _init_vndb_catalogue(self):
        self._vndb_status = 'VNDB在线目录尚未开放或尚未同步'
        self._vndb_sync_pending = False
        try:
            self._vndb_cover_index = online.get_cover_index(self.conn)
        except ValueError:
            self._vndb_cover_index = {}
            self._vndb_status = 'VNDB封面索引不可用，可更新目录重试'
        self._vndb_cover_cache = online.CoverCache(slg_db.covers_dir(), self._fetch_dlsite_cover)
        from pathlib import Path
        from slg_vndb_official_images import OfficialImageCache
        self._vndb_official_cache = OfficialImageCache(Path(slg_db.covers_dir()) / 'vndb-official')
        self._reload_vndb_official_references()

    def _maybe_update_vndb(self):
        if (not self._network_enabled or self.busy or self._vndb_sync_pending
                or not online.should_check(self.conn)
                or time.monotonic()-getattr(self, '_vndb_failed_check', -3600) < 3600):
            return
        self._vndb_sync_pending = True
        identity = self._library_identity()
        def complete(value, error):
            self._vndb_sync_pending = False
            if error:
                self._vndb_failed_check = time.monotonic()
                self._vndb_status = 'VNDB在线目录暂不可用，保留本机资料'
                self._log('VNDB更新未完成，保留已有目录：' + str(error))
            else:
                self._vndb_cover_index = online.get_cover_index(self.conn)
                self._reload_vndb_official_references()
                self._vndb_status = 'VNDB在线目录 · %d款' % value['catalogue']
                self._catalogue_row_cache.clear()
                self._invalidate_cards()
            if identity == self._library_identity() and self.catalogue_id in ('vndb', 'all'):
                self.refresh(preserve_scroll=True)
        def worker():
            import slg_sync_server
            try:
                with slg_db.session() as conn:
                    value = slg_sync_server.pull(conn, catalog_id='vndb', log=self._log,
                        should_stop=self._closing_event.is_set)
                error = None
            except Exception as exc:
                value, error = None, exc
            if not self._closing_event.is_set():
                self.queue.put(('library_callback', (complete, value, error)))
        self._dlsite_executor.submit(worker)

    def _reload_vndb_official_references(self):
        import json
        from slg_vndb_official_images import validate_official_index
        try:
            raw = slg_db.get_pref(self.conn, online.OFFICIAL_INDEX_KEY)
            self._vndb_official_index = validate_official_index({
                'version':1, 'catalog_id':'vndb', 'items':json.loads(raw) if raw else {}})['items']
        except (ValueError,TypeError):
            self._vndb_official_index = {}
            self._vndb_status = 'VNDB官方封面索引不可用，可更新目录重试'

    def _prune_vndb_official_requests(self):
        cache = getattr(self, '_vndb_official_cache', None)
        consumers = getattr(self, '_cover_consumers', None)
        if cache is None or consumers is None:
            return
        consumers.prune()
        for key, widgets in list(consumers._pending.items()):
            if not widgets:
                cache.cancel_consumer(key)

    def _bind_vndb_official_cover(self, widget, game, reference, key, width, height):
        from concurrent.futures import CancelledError
        from PIL import Image
        from slg_vndb_official_images import reference_hidden
        widget._slg_official_game = dict(game)
        generation = getattr(self, '_card_render_generation', 0)
        allow_network = self._network_enabled
        hide = slg_db.get_pref(self.conn, 'vndb.hide_sensitive_covers') == '1'
        cache = self._vndb_official_cache
        def decode():
            try:
                future = cache.request(game['slug'], reference, hide_sensitive=hide,
                    allow_network=allow_network, consumer=key)
                path = future.result()
                if not path:
                    return None, '敏感封面已隐藏' if reference_hidden(reference, hide) else 'VNDB封面未缓存，联网后可按需下载'
                with Image.open(path) as source:
                    image = source.convert('RGB')
                    image.thumbnail((width*2, height*2), Image.Resampling.LANCZOS)
                return image, None if cache.is_current(game['slug'], reference) else '_official_stale'
            except CancelledError:
                return None, '_official_cancelled'
            except Exception:
                return None, 'VNDB官方封面暂不可用，稍后重试'
        def publish(result_key, result):
            if not self._closing_event.is_set():
                image, reason = result if isinstance(result,tuple) else (None,'VNDB封面暂不可用')
                self.queue.put(('cover_decoded', (result_key,image,reason)))
        if not self._cover_consumers.request(key, widget, generation,
                self._download_executor, decode, publish):
            self._cover_retry[widget] = (dict(game),width,height,key)
            self._schedule_cover_retry()

    def _set_sensitive_covers(self, enabled):
        slg_db.set_pref(self.conn, 'vndb.hide_sensitive_covers', '1' if enabled else '0')
        self.conn.commit()
        self._invalidate_cards()
        self._detail_sig = None
        self.refresh(preserve_scroll=True)
