"""Local-library dialogs. Workers prepare files; every Tk/SQLite write stays on Tk."""
import os
import time
import sqlite3
from pathlib import Path
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from slg_image_resources import ResourceLabel, ResourceButton, ResourceScrollableFrame
import slg_db
import slg_local_library as library
import slg_dlsite_client as dlsite
from slg_source_status import status_note


def _option_menu_style():
    """Bind both appearance states explicitly, including retained dialogs."""
    import slg_gui as g
    pair = lambda name: (g._LIGHT[name], g._DARK[name])
    return dict(fg_color=pair("CHIP"), text_color=pair("TEXT"),
        text_color_disabled=pair("MUTED"), button_color=pair("CHIP"),
        button_hover_color=pair("CARD_HOVER"), dropdown_fg_color=pair("CARD"),
        dropdown_hover_color=pair("CARD_HOVER"), dropdown_text_color=pair("TEXT"))


class LibraryUI:
    def _fit_library_card(self, slot):
        if self.origin != "local" or not slot["frame"].winfo_exists():
            return
        slot["library_wrapped"] = True
        card = slot["frame"]
        width = card.winfo_width()
        if width <= 1:
            return
        if slot.get("wrap_width") == width:
            return
        slot["wrap_width"] = width
        usable = max(150, width - slot["img"].winfo_reqwidth() - card._apply_widget_scaling(42))
        for name in ("title", "meta", "tagline"):
            label = slot[name]
            if int(label.cget("wraplength")) != int(usable):
                label.configure(wraplength=int(usable), justify="left")

    def _library_identity(self):
        return self._capture_identity_context()

    def _library_personal_changed(self):
        """Invalidate snapshots after a successful personal write on the Tk thread."""
        self._library_personal_revision = getattr(self, "_library_personal_revision", 0) + 1
        self._library_query_generation += 1
        self._card_render_generation = getattr(self, "_card_render_generation", 0) + 1

    def _library_async(self, win, task, success, status=None, channel="default", on_error=None):
        """One generation per dialog; closed/replaced/identity-changed results die."""
        token = uuid.uuid4().hex
        if not hasattr(win, "_library_tokens"):
            win._library_tokens = {}
        win._library_tokens[channel] = token
        identity = self._library_identity()
        def complete(value, error):
            if (not win.winfo_exists() or getattr(win, "_library_tokens", {}).get(channel) != token
                    or identity != self._library_identity()):
                return
            if error:
                if status is not None:
                    status.configure(text="处理失败：" + str(error))
                if on_error is not None:
                    on_error(error)
                return
            success(value)
        def worker():
            try:
                value, error = task(), None
            except Exception as exc:
                value, error = None, exc
            self.queue.put(("library_callback", (complete, value, error)))
        self._cover_executor.submit(worker)

    def _library_path_status(self, path, game_id):
        if not path:
            return "未绑定目录"
        cache = getattr(self, "_library_path_cache", None)
        if cache is None:
            self._library_path_cache = cache = {}
        times = getattr(self, "_library_path_times", {})
        self._library_path_times = times
        if path not in cache or (time.monotonic() - times.get(path, 0) > 30):
            cache[path] = "正在检查路径"
            times[path] = time.monotonic()
            def inspect():
                value = "路径正常" if os.path.exists(path) else "路径需修复"
                self.queue.put(("library_path", (path, game_id, value)))
            self._path_executor.submit(inspect)
        return cache[path]

    def toggle_library_source(self):
        self.library_source = "all" if self.library_source == "user" else "user"
        self.category_ids, self.category_uncategorized = [], False
        self.page = 1
        self.refresh()

    def _maybe_update_dlsite(self):
        import slg_dlsite_online
        if (not self._network_enabled or self.busy or getattr(self,"_dlsite_sync_pending",False)
                or not slg_dlsite_online.should_check(self.conn)
                or time.monotonic()-getattr(self,"_dlsite_failed_check",-3600) < 3600):
            return
        self._dlsite_sync_pending = True
        identity = self._library_identity()
        def complete(value,error):
            self._dlsite_sync_pending = False
            if error:
                self._dlsite_failed_check = time.monotonic()
                self._dlsite_pilot_status = "离线缓存 · 更新暂不可用"
                self._log("DLsite更新失败，保留已有目录：" + str(error))
            else:
                self._dlsite_cover_index = slg_dlsite_online.get_cover_index(self.conn)
                self._catalogue_row_cache.clear()
                self._invalidate_cards()
                self._dlsite_pilot_status = "在线目录 · %d款" % value["catalogue"]
                note = status_note(value.get("source_status"))
                if note:
                    self._dlsite_pilot_status += " · " + note
            if identity == self._library_identity() and (self.catalogue_id in ("dlsite", "all") or self.origin == "local"):
                self.refresh(preserve_scroll=True)
        def job():
            import slg_sync_server
            try:
                with slg_db.session() as conn:
                    value = slg_sync_server.pull(conn,catalog_id="dlsite",log=self._log,should_stop=self._closing_event.is_set)
                error = None
            except Exception as exc:
                value,error = None,exc
            self.queue.put(("library_callback",(complete,value,error)))
        self._dlsite_executor.submit(job)

    def _request_library_page(self, preserve_scroll=False, skip_if_same=False):
        from slg_library_query import query_local_page
        getattr(self, "_navigation_query_started", lambda: None)()
        self._library_query_generation += 1
        generation = self._library_query_generation
        identity = self._library_identity()
        spec = dict(search=self.search, include=tuple(self.include), exclude=tuple(self.exclude),
                    source=self.library_source, category_ids=tuple(self.category_ids),
                    uncategorized=self.category_uncategorized, sort=self.sort, desc=self.sort_desc,
                    date_preset=self.date_preset, date_start=self.date_start, date_end=self.date_end)
        page = self.page
        revision = getattr(self, "_library_personal_revision", 0)
        size = __import__("slg_gui").PAGE_SIZE
        database = self.conn.execute("PRAGMA database_list").fetchone()[2]
        focus = getattr(self, "_library_focus_gid", None)
        spec["focus_id"] = focus
        self._library_focus_gid = None
        self._library_query_pending = True
        self._card_render_pending = True
        offset = self._scroll_offset() if preserve_scroll else None
        previous = getattr(self, "_library_query_future", None)
        if previous is not None:
            previous.cancel()
        def done(result, error):
            if (self.origin != "local" or generation != self._library_query_generation
                    or revision != getattr(self, "_library_personal_revision", 0)
                    or identity != self._library_identity()):
                return
            self._library_query_pending = False
            if error:
                getattr(self, "_navigation_query_completed", lambda **kw: False)(error=True)
                self._card_render_pending = False
                self._log("本地库读取失败：" + str(error))
                return
            self.rows, self._library_total, self.page = result.rows, result.total, result.page
            self._library_last_query_timings = dict(result.timings)
            self._library_row_index = {g["id"]: g for g in self.rows}
            if self.selected and self.selected["id"] in self._library_row_index:
                self.selected = dict(self._library_row_index[self.selected["id"]])
            visible = [g["id"] for g in self.rows]
            self._sync_cards(self.rows)
            self._render_pager()
            self._set_empty_label(not self.rows)
            self._rendered_ids = visible
            if offset is not None:
                self._restore_scroll(offset)
            self.stat_label.configure(text="本地库 %d 款 · 本页 %d 款" % (result.total, len(self.rows)))
            navigation_handled = getattr(self, "_navigation_query_completed", lambda: False)()
            if not navigation_handled and focus is None:
                self._render_detail_if_stale()
            if not navigation_handled and focus is not None and focus in self._library_row_index:
                self.select(self._library_row_index[focus])
            edge = getattr(self,"_library_selection_edge",None)
            self._library_selection_edge = None
            if not navigation_handled and edge is not None and self.rows:
                self.select(self.rows[edge])
                self._scroll_card_into_view(self.rows[edge]["id"])
        def query():
            conn = None
            try:
                conn = sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                result, error = query_local_page(conn, spec, page, size), None
            except Exception as exc:
                result, error = None, exc
            finally:
                if conn is not None:
                    conn.close()
            self.queue.put(("library_callback", (done, result, error)))
        self._library_query_future = self._query_executor.submit(query)
        self._wake_queue()

    def set_library_view(self):
        self._leave_panel_workspace()
        self._leave_collections_view()
        self._catalogue_query_pending = False
        if self.origin == "local":
            return
        self._leave_user_view()
        self._catalogue_states[self.catalogue_id] = self._capture_catalogue_state()
        state = getattr(self, "_library_view_state", None)
        if state:
            self._restore_catalogue_state(state)
        else:
            self.search = ""
            self.date_preset, self.date_start, self.date_end = "all", None, None
            self.search_entry.delete(0, "end")
            self.include, self.exclude = [], []
            self.selected = None
            self.sort, self.sort_desc, self.page = "title", False, 1
        self.origin, self.view, self.collection_id = "local", None, None
        for button in self.view_buttons.values():
            button.configure(fg_color="transparent")
        self._paint_user_view()
        self.refresh()
        self.after_idle(self._offer_library_onboarding)

    def _offer_library_onboarding(self):
        if self.origin != "local" or getattr(self, "_library_intro_open", False):
            return
        if slg_db.get_pref(self.conn, "library.onboarding_seen"):
            return
        from slg_gui import CARD, CARD_HOVER, ACCENT_HOVER, TEXT, MUTED, CHIP, ACCENT, ON_ACCENT
        self._library_intro_open = True
        win = self._new_dialog("建立你的本地游戏库", "440x240")
        ResourceLabel(win, text="选择一个或多个游戏目录，扫描后确认识别结果。\n也可以稍后再添加，入口一直保留在本地库。",
            text_color=MUTED, wraplength=390).pack(padx=20,pady=25)
        def finish(configure=False):
            slg_db.set_pref(self.conn, "library.onboarding_seen", "1")
            self._library_intro_open = False
            win.destroy()
            if configure:
                self.open_library_import()
        win.protocol("WM_DELETE_WINDOW", finish)
        ResourceButton(win,text="选择扫描路径",command=lambda:finish(True), fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT, text_color_disabled=ON_ACCENT).pack(fill="x",padx=24,pady=6)
        ResourceButton(win,text="稍后再说",fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED,command=finish).pack(fill="x",padx=24,pady=6)

    def _library_refresh_game(self, game_id):
        """Update bound cards; restart any outstanding snapshot before repaint."""
        pending = getattr(self, "_library_query_pending", False) or getattr(self, "_card_render_pending", False)
        self._library_personal_changed()
        if pending:
            self.refresh(preserve_scroll=True)
        row = self.conn.execute(
            "SELECT g.*, l.folder_path,l.folder_version,l.exe_path FROM games g"
            " LEFT JOIN local l ON l.game_id=g.id WHERE g.id=?", (game_id,)).fetchone()
        if row is None:
            return
        base = library.apply_cover_overrides(self.conn, [dict(row)])[0]
        for game in self.rows:
            if game["id"] == game_id:
                game.update(base)
        slot = self._card_slot.get(game_id)
        if slot:
            game = next((g for g in self.rows if g["id"] == game_id), None)
            if game is not None:
                self._fill_card(slot, game, slg_db.game_tags(self.conn, game_id))
        if self.selected is not None and self.selected["id"] == game_id:
            self.selected.update(base)
            self._detail_sig = None
            self._render_detail()

    def open_cover_editor(self, game):
        if not self._require_personal_access("更换个人封面"):
            return
        from slg_gui import CARD, CARD_HOVER, ACCENT_HOVER, TEXT, MUTED, ACCENT, ON_ACCENT, CHIP, ui_font
        game = dict(game)
        win = self._new_dialog("个人封面", "460x640")
        identity = self._library_identity()
        ResourceLabel(win, text=self._title_to_show(game), text_color=TEXT,
                     wraplength=410, font=ui_font(size=15, weight="bold")).pack(padx=20, pady=12)
        preview = ResourceLabel(win, text="")
        preview.pack(padx=20, pady=8)
        self._bind_cover(preview, game, 260, 280)
        status = ResourceLabel(win, text="选择本地图片，预览后保存。仅影响你的客户端。",
                              wraplength=410, text_color=MUTED)
        status.pack(padx=20, pady=8)
        state = {"image": None, "ready": False, "saving": False}
        save = ResourceButton(win, text="保存封面", state="disabled",
                             fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color_disabled=ON_ACCENT, text_color=ON_ACCENT)
        def prepare(task):
            if state["saving"] or identity != self._library_identity():
                return
            state.update(image=None, ready=False)
            save.configure(state="disabled")
            status.configure(text="正在读取并验证图片…")
            def prepared(image):
                state.update(image=image, ready=True)
                thumb = image.copy()
                thumb.thumbnail((520, 560))
                photo = ctk.CTkImage(light_image=thumb, size=(thumb.width // 2, thumb.height // 2))
                preview._slg_cover_key = None
                preview.configure(image=photo)
                preview._library_image = photo
                status.configure(text="预览已就绪，保存后生效。")
                save.configure(state="normal")
            self._library_async(win, task, prepared, status, channel="cover-prepare")
        def choose():
            if state["saving"]:
                return
            path = filedialog.askopenfilename(parent=win, title="选择封面图片",
                    filetypes=[("图片", "*.jpg *.jpeg *.png *.webp")])
            if path:
                prepare(lambda: library.prepare_cover(path))
        def paste(event=None):
            if not state["saving"] and identity == self._library_identity():
                from slg_cover_clipboard import prepare_clipboard_cover
                prepare(prepare_clipboard_cover)
            return "break"
        def commit():
            if (identity != self._library_identity() or not state["ready"]
                    or not self._require_personal_access("保存个人封面")):
                return
            state["ready"] = False
            state["saving"] = True
            prepared = state["image"]
            save.configure(state="disabled")
            status.configure(text="正在保存…")
            def failed(_error):
                state.update(ready=True, saving=False)
                save.configure(state="normal")
            def saved(name):
                if not self._require_personal_access("保存个人封面"):
                    return
                try:
                    library.set_cover_override(self.conn, game["id"], name)
                    self.conn.commit()
                except Exception as exc:
                    self.conn.rollback()
                    status.configure(text="保存失败：" + str(exc))
                    failed(exc)
                    return
                self._library_refresh_game(game["id"])
                win.destroy()
            self._library_async(win, lambda: library.save_cover(prepared), saved, status, on_error=failed)
        def reset():
            if state["saving"] or identity != self._library_identity():
                return
            if self._require_personal_access("恢复默认封面"):
                try:
                    library.clear_cover_override(self.conn, game["id"])
                    self.conn.commit()
                except Exception as exc:
                    self.conn.rollback()
                    status.configure(text="恢复失败：" + str(exc))
                    return
                self._library_refresh_game(game["id"])
                win.destroy()
        ResourceButton(win, text="选择图片…", command=choose, fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED).pack(fill="x", padx=22, pady=5)
        ResourceButton(win, text="从剪贴板粘贴图片 · Ctrl+V", command=paste,
                       fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT,
                       text_color_disabled=MUTED).pack(fill="x", padx=22, pady=5)
        win.bind("<Control-v>", paste, add="+")
        win.bind("<Control-V>", paste, add="+")
        save.configure(command=commit)
        save.pack(fill="x", padx=22, pady=5)
        ResourceButton(win, text="恢复默认封面", command=reset, fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED).pack(fill="x", padx=22, pady=5)
        ResourceButton(win, text="取消", command=win.destroy, fg_color="transparent", hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED).pack(fill="x", padx=22, pady=5)
        def online_cover():
            if state["saving"] or identity != self._library_identity():
                return
            win.destroy()
            self.open_library_cover_query(game)
        ResourceButton(win, text="联网查找封面…", command=online_cover, fg_color=CHIP,
                       hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED
                       ).pack(fill="x", padx=20, pady=4)
        self._cover_editor = {"win": win, "choose": choose, "save": commit, "reset": reset,
                              "online": online_cover, "paste": paste, "prepare": prepare,
                              "save_button": save, "preview": preview, "status": status, "state": state}

    def open_local_binding(self, game):
        if not self._require_personal_access("管理本地绑定"):
            return
        from slg_gui import CARD, CARD_HOVER, ACCENT_HOVER, TEXT, MUTED, CHIP, ACCENT, ON_ACCENT
        identity = self._library_identity()
        win = self._new_dialog("本地目录与启动程序", "480x360")
        path = ResourceLabel(win, text="目录：%s\n启动程序：%s" %
                   (game.get("folder_path") or "未设置", game.get("exe_path") or "未设置"),
                   text_color=TEXT, wraplength=430, justify="left")
        path.pack(fill="x", padx=22, pady=22)
        status = ResourceLabel(win, text="", text_color=MUTED, wraplength=430)
        status.pack(fill="x", padx=22)
        def folder():
            chosen = filedialog.askdirectory(parent=win, title="选择游戏目录")
            if not chosen or identity != self._library_identity() or not self._require_personal_access("设置本地目录"):
                return
            try:
                import slg_scan
                key = slg_scan.normalize_path(chosen)
                for row in self.conn.execute("SELECT game_id,folder_path FROM local WHERE folder_path IS NOT NULL"):
                    if row["game_id"] != game["id"] and slg_scan.normalize_path(row["folder_path"]) == key:
                        raise ValueError("该目录已登记到其他游戏，请先解除原绑定。")
                slg_db.set_local_folder(self.conn, game["id"], os.path.normpath(chosen))
                self.conn.commit()
                self._library_personal_changed()
            except Exception as exc:
                self.conn.rollback()
                status.configure(text="保存失败：" + str(exc))
                return
            win.destroy()
            self.refresh(preserve_scroll=True)
            bound = self.conn.execute("SELECT exe_path FROM local WHERE game_id=?", (game["id"],)).fetchone()
            if game.get("exe_path") and bound and not bound["exe_path"]:
                messagebox.showinfo("请重新选择启动程序", "目录已更换，原启动程序不属于新目录，请重新选择。", parent=self)
        def exe():
            if identity != self._library_identity():
                return
            chosen = self._pick_game_exe(game["id"], game.get("folder_path"))
            if chosen:
                win.destroy()
                self.refresh(preserve_scroll=True)
        def clear(folder_too):
            if identity != self._library_identity() or not self._require_personal_access("解除本地绑定"):
                return
            try:
                library.clear_local_binding(self.conn, game["id"], clear_folder=folder_too, clear_exe=True)
                self.conn.commit()
                self._library_personal_changed()
            except Exception as exc:
                self.conn.rollback()
                status.configure(text="解除失败：" + str(exc))
                return
            win.destroy()
            self.refresh(preserve_scroll=True)
        for text, command in (("选择／修复游戏目录", folder), ("选择启动程序", exe),
                              ("清除启动程序", lambda: clear(False)),
                              ("解除本地登记", lambda: clear(True))):
            ResourceButton(win, text=text, command=command, fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED).pack(fill="x", padx=22, pady=5)
        ResourceLabel(win, text="解除登记不会删除磁盘上的游戏文件。", text_color=MUTED).pack(pady=8)

    def open_dlsite_filters(self):
        from slg_gui import CARD, CARD_HOVER, ACCENT_HOVER, TEXT, MUTED, ACCENT, ON_ACCENT
        win = self._new_dialog("DLsite 语言与作品形式", "440x520")
        body = ResourceScrollableFrame(win, fg_color=CARD)
        body.pack(fill="both", expand=True, padx=16, pady=16)
        options = dlsite.filter_options(self.conn)
        variables = {}
        for key, label, current in (("languages", "作品语言", self.dlsite_languages),
                                   ("work_types", "作品形式", self.dlsite_work_types)):
            ResourceLabel(body, text=label, text_color=TEXT).pack(anchor="w", pady=(8, 3))
            values = tuple(options[key])
            if "__unknown__" not in values:
                values += ("__unknown__",)
            variables[key] = {value: tk.BooleanVar(value=value in current) for value in values}
            for value, var in variables[key].items():
                self._tag_checkbox(body, "未知" if value == "__unknown__" else value, var).pack(anchor="w", pady=3)
        ResourceLabel(body, text="同组满足任一项，不选表示全部。", text_color=MUTED).pack(pady=10)
        def apply():
            self.dlsite_languages = [v for v, var in variables["languages"].items() if var.get()]
            self.dlsite_work_types = [v for v, var in variables["work_types"].items() if var.get()]
            self.page = 1
            win.destroy()
            self.refresh()
        ResourceButton(win, text="应用", command=apply, fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color_disabled=ON_ACCENT,
                     text_color=ON_ACCENT).pack(fill="x", padx=16, pady=(0, 16))

    def open_scan_review(self):
        """Manual review never silently binds games or queries the network."""
        if self.busy or not self._require_personal_access("扫描待处理"):
            return
        import slg_scan_review as review
        import slg_scan_targets
        from slg_gui import CARD, TEXT, MUTED, CHIP, CARD_HOVER
        plans = slg_scan_targets.load_targets(self.conn)
        win = self._new_dialog("扫描待处理", "680x580")
        identity = self._library_identity()
        ResourceLabel(win, text="选择失败或未匹配目录后重试；忽略可撤销，不删除文件。", text_color=TEXT).pack(pady=12)
        status = ResourceLabel(win, text="", text_color=MUTED, wraplength=630)
        status.pack(padx=16)
        body = ResourceScrollableFrame(win, fg_color=CARD)
        body.pack(fill="both", expand=True, padx=16, pady=10)
        states = []
        from slg_motion import MotionScheduler, TimeBudget
        scheduler = MotionScheduler(body)
        generation = [0]
        labels = {"unmatched":"未匹配", "ambiguous":"待确认", "conflict":"绑定冲突", "read_failed":"读取失败", "query_failed":"查询失败"}
        def redraw():
            generation[0] += 1
            token = generation[0]
            for widget in body.winfo_children():
                widget.destroy()
            states.clear()
            rows = [r for r in review.get_recent(self.conn, plans) if r["status"] != "matched"]
            status.configure(text="%d 项待处理（含已忽略项）。" % len(rows) if rows else "暂无待处理记录，请先扫描。")
            cursor = [0]
            def batch():
                if token != generation[0]:
                    return
                budget = TimeBudget(milliseconds=6, max_items=5)
                while cursor[0] < len(rows) and budget.available:
                    row = rows[cursor[0]]
                    var = tk.BooleanVar(value=False)
                    text = ("[已忽略] " if row["ignored"] else "") + labels.get(row["status"], row["status"]) + " · " + row["path"]
                    if row["message"]:
                        text += "\n" + row["message"]
                    self._tag_checkbox(body, text, var).pack(fill="x", padx=8, pady=5)
                    states.append((row, var))
                    cursor[0] += 1
                    budget.consumed()
                if cursor[0] < len(rows):
                    scheduler.call_later(1, batch)
            batch()
        def action(kind):
            if identity != self._library_identity() or self.busy:
                return
            selected = [row["path"] for row,var in states if var.get()]
            if not selected:
                status.configure(text="请先选择目录。")
                return
            if kind != "retry":
                getattr(review, kind)(self.conn, plans, selected)
                self.conn.commit()
                redraw()
                return
            targets = review.retry_selection(self.conn, plans, selected)
            originals = {review.path_key(p["path"]):p for p in plans}
            expanded = []
            for target in targets:
                key = review.path_key(target["path"])
                if key in originals:
                    expanded.append(dict(originals[key]))
                    continue
                parents = []
                for root,plan in originals.items():
                    try:
                        if os.path.commonpath((key,root)) == root:
                            parents.append((len(root),plan))
                    except ValueError:
                        pass
                inherited = max(parents,key=lambda pair:pair[0])[1] if parents else {}
                expanded.append({**inherited,**target})
            targets = expanded
            if not targets:
                status.configure(text="所选条目已忽略，请先撤销忽略。")
                return
            win.destroy()
            self._run_job("重试所选目录…", self._scan_worker, targets, "children", self._library_identity(), False, plans)
        redraw()
        actions = ctk.CTkFrame(win, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=10)
        for text,kind in (("重试所选", "retry"), ("忽略所选", "ignore"), ("撤销忽略", "unignore")):
            ResourceButton(actions, text=text, command=lambda k=kind:action(k), fg_color=CHIP, hover_color=CARD_HOVER,
                           text_color=TEXT, text_color_disabled=MUTED).pack(side="left", padx=4)
        self._scan_review_dialog = dict(win=win, states=states, action=action, status=status)

    def open_library_import(self):
        if not self._require_personal_access("导入本地游戏") or self.busy:
            return
        import slg_scan_targets
        from slg_scan_target_layout import ScanTargetRow
        from slg_gui import CARD, CHIP, MUTED, TEXT, CARD_HOVER, ACCENT, ON_ACCENT, ACCENT_HOVER, ui_font
        win = self._new_dialog("本地库扫描路径", "780x570")
        identity = self._library_identity()
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(3, weight=1)
        ResourceLabel(win, text="自动找游戏根可拆分合集，默认最多4层；更深扫描可选到10层。\n固定层级和单款模式仍可使用。扫描后确认才登记，联网查询需主动选择。",
                     text_color=MUTED, font=ui_font(13), wraplength=520).grid(row=0,column=0,sticky="ew",padx=16,pady=16)
        status = ResourceLabel(win, text="", text_color=MUTED, font=ui_font(13), wraplength=520)
        status.grid(row=1,column=0,sticky="ew",padx=16)
        force_redetect = tk.BooleanVar(value=False)
        self._tag_checkbox(win, "完整重新识别（引擎和启动程序）",
                           force_redetect).grid(row=2,column=0,sticky="w",padx=20,pady=6)
        try:
            targets = slg_scan_targets.load_targets(self.conn)
        except ValueError as exc:
            targets = []
            status.configure(text="旧配置无效，请重新选择：" + str(exc))
        body = ResourceScrollableFrame(win, fg_color=CARD)
        body.grid(row=3,column=0,sticky="nsew",padx=16,pady=8)
        def redraw():
            for child in body.winfo_children():
                child.destroy()
            for item in targets:
                row = ScanTargetRow(body, path=item["path"], fg_color=CARD, text_color=TEXT, font=ui_font(14))
                row.pack(fill="x",pady=4)
                mode_labels = {"直系子目录": "children", "单款游戏目录": "single", "自动找游戏根": "discover"}
                menu = ctk.CTkOptionMenu(row.controls, **_option_menu_style(), font=ui_font(14), dropdown_font=ui_font(14),values=list(mode_labels))
                menu.set(next(label for label, mode in mode_labels.items() if mode == item["mode"]))
                options = [menu]
                def change_mode(value, t=item):
                    old_mode = t["mode"]
                    t["mode"] = mode_labels[value]
                    t["depth"] = (0 if t["mode"] == "single" else
                        4 if t["mode"] == "discover" and old_mode != "discover" else
                        min(4, max(1, t.get("depth", 1))) if t["mode"] == "children" else max(1, t.get("depth", 4)))
                    redraw()
                menu.configure(command=change_mode)
                if item["mode"] != "single":
                    depth = ctk.CTkOptionMenu(row.controls, **_option_menu_style(), font=ui_font(14), dropdown_font=ui_font(14), values=["第%d层" % n for n in range(1, 11 if item["mode"] == "discover" else 5)],
                        command=lambda value,t=item:t.update(depth=int(value[1:-1])))
                    depth.set("第%d层" % item.get("depth", 1))
                    options.append(depth)
                sources = ctk.CTkOptionMenu(row.controls, **_option_menu_style(), font=ui_font(14), dropdown_font=ui_font(14), values=["按需选择来源", "优先DLsite", "优先VNDB"],
                    command=lambda value,t=item:t.update(online_sources=["dlsite"] if value=="优先DLsite" else ["vndb"] if value=="优先VNDB" else []))
                sources.set("优先DLsite" if item.get("online_sources")==["dlsite"] else "优先VNDB" if item.get("online_sources")==["vndb"] else "按需选择来源")
                options.append(sources)
                collection_values = {"不加入收藏夹": None}
                collection_values.update({c["name"]:c["id"] for c in slg_db.list_collections(self.conn)})
                collection = ctk.CTkOptionMenu(row.controls, **_option_menu_style(), font=ui_font(14), dropdown_font=ui_font(14), values=list(collection_values), width=130,
                    command=lambda value,t=item,v=collection_values:t.update(target_collection_id=v[value]))
                collection.set(next((k for k,v in collection_values.items() if v==item.get("target_collection_id")),"不加入收藏夹"))
                options.append(collection)
                def remove(t=item):
                    targets.remove(t)
                    redraw()
                ResourceButton(row.actions,text="移除目标",font=ui_font(14),width=90,fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED,command=remove).pack(side="right",padx=10,pady=6)
                row.set_controls(options)
        def choose():
            root = filedialog.askdirectory(parent=win,title="添加扫描目标路径")
            if root:
                try:
                    targets[:] = slg_scan_targets.normalize_targets(targets + [{"path":root,"mode":"discover", "depth":4}])
                    redraw()
                except ValueError as exc:
                    status.configure(text=str(exc))
        def save(start=False):
            if identity != self._library_identity():
                return
            try:
                cleaned = slg_scan_targets.save_targets(self.conn,targets)
                if cleaned is None:
                    cleaned = slg_scan_targets.normalize_targets(targets)
            except ValueError as exc:
                status.configure(text=str(exc))
                return
            if start and not cleaned:
                status.configure(text="请至少添加一个目标路径。")
                return
            win.destroy()
            if start:
                self._run_job("识别本地游戏…",self._scan_worker,cleaned,"children",
                              self._library_identity(),force_redetect.get())
        redraw()
        actions = ctk.CTkFrame(win,fg_color="transparent")
        actions.grid(row=4,column=0,sticky="ew",padx=16,pady=14)
        actions.grid_columnconfigure((0, 1),weight=1,uniform="scan-actions")
        for label,command in (("添加路径…",choose),("保存路径",save),("开始扫描",lambda:save(True))):
            button = ResourceButton(actions,text=label,font=ui_font(14),width=120,command=command, fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT, text_color_disabled=ON_ACCENT)
            index = len(actions.winfo_children()) - 1
            button.grid(row=index // 2,column=index % 2,sticky="ew",padx=6,pady=4)
        self._library_targets_dialog = dict(win=win,targets=targets,choose=choose,save=save,
                                            status=status,force_redetect=force_redetect)

    def open_library_scan_results(self, result):
        if result.get("cancelled"):
            return
        from slg_gui import CARD, CARD_HOVER, ACCENT_HOVER, TEXT, MUTED, CHIP, ACCENT, ON_ACCENT, FlowFrame
        import slg_scan
        items = result.get("items", [])
        identity = self._library_identity()
        win = self._new_dialog("确认本地游戏导入", "680x600")
        staged_covers = set()
        def discard_staged(event):
            if event.widget != win:
                return
            # Delete only files explicitly transferred as newly-created previews.
            for name in staged_covers:
                if not library._valid_filename(name):
                    continue
                if self.conn.execute("SELECT 1 FROM game_cover_overrides WHERE cover_file=?",(name,)).fetchone():
                    continue
                try:
                    os.unlink(os.path.join(slg_db.covers_dir(),name))
                except OSError:
                    pass
        win.bind("<Destroy>", discard_staged, add="+")
        ResourceLabel(win, text="目录已有作品选匹配，未收录选联网补充；仍无结果可手动添加。\n确认导入后才会写入。",
                      text_color=TEXT, wraplength=620).pack(padx=16, pady=12)
        body = ResourceScrollableFrame(win, fg_color=CARD)
        body.pack(fill="both", expand=True, padx=16, pady=8)
        states = []
        status = ResourceLabel(win, text="\n".join(str(e) for e in result.get("errors", []) + result.get("warnings", [])),
                             text_color=MUTED, wraplength=620)
        status.pack(padx=16, pady=4)
        def build_item(item):
            row = ctk.CTkFrame(body, fg_color=CARD)
            row.pack(fill="x", pady=4)
            var = tk.BooleanVar(value=bool(item.get("game_id") and
                item.get("status") not in ("conflict", "ambiguous") and
                not item.get("identity_warning")))
            label = "%s\n%s" % (item["name"], item["path"])
            check = self._tag_checkbox(row, label, var)
            check.pack(fill="x", padx=10, pady=8)
            reasons = {"existing_path": "保留已有路径绑定", "exact": "名称匹配",
                       "contained": "名称相近，请核对", "fuzzy": "名称相近，请核对",
                       "ambiguous": "发现多个同名候选，请选择绑定游戏",
                       "unmatched": "未找到匹配，可添加为自建游戏",
                       "existing_other_path": "已有另一个目录，请确认是否替换",
                       "product_id": "RJ 编号精确匹配",
                       "unknown_product_id": "目录未收录此编号，可手动选择或添加自建游戏",
                       "unsupported_product_id": "当前目录尚不支持此商品编号"}
            text = reasons.get(item.get("reason"), "请核对识别结果")
            if item.get("engine") or item.get("engine_variant"):
                text += "\n引擎线索：" + (item.get("detected_engine") or item.get("engine") or "未知")
                if item.get("engine_variant"):
                    text += " · " + item["engine_variant"]
                if item.get("detected_engine") and item.get("engine") and item["detected_engine"] != item["engine"]:
                    text += "（保留当前已选引擎：" + item["engine"] + "）"
            if item.get("product_ids"):
                text += " · " + " / ".join(item["product_ids"])
            if item.get("identity_warning"):
                text += "\n" + item["identity_warning"]
            for fact in item.get("evidence", {}).get("titles", [])[:3]:
                text += "\n文件证据：" + fact["text"] + "（" + fact["reason"] + "）"
            if item.get("detection_mode") == "quick_check":
                text += "\n已有启动路径有效，已快速检查；需要检测变化时请选择完整重新识别。"
            note = ResourceLabel(row, text=text, text_color=MUTED, wraplength=580)
            note.pack(anchor="w", padx=10)
            entry = {"item": dict(item), "var": var, "exe": None}
            states.append(entry)
            actions = FlowFrame(row, fg_color="transparent", gap_x=6, gap_y=6)
            actions.pack(fill="x", padx=10, pady=6)
            action_buttons = []
            def adopt(candidate):
                if not win.winfo_exists() or identity != self._library_identity():
                    return False
                import slg_library_metadata
                known = slg_library_metadata.find_existing(self.conn, candidate["source"], candidate["external_id"])
                entry["item"].update(game_id=known, new=known is None, provider_metadata=candidate)
                if candidate.get("personal_cover_owned") and candidate.get("personal_cover_file"):
                    staged_covers.add(candidate["personal_cover_file"])
                entry["var"].set(True)
                note.configure(text="将采用：%s · %s；确认导入后保存" % (candidate["title"], candidate["external_id"]))
                return True
            action_buttons.append(ResourceButton(actions, text="联网查找并补充资料…", command=lambda:self.open_metadata_query(entry["item"],adopt),
                width=210, height=38, fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED))
            def bind():
                def selected(game):
                    if not win.winfo_exists() or identity != self._library_identity():
                        return
                    entry["item"].pop("provider_metadata", None)
                    entry["item"]["game_id"] = game["id"]
                    entry["item"]["new"] = False
                    entry["var"].set(True)
                    note.configure(text="绑定到：" + game["title"])
                picker = self._pick_game(selected, candidates=item.get("candidates", ()))
                win.bind("<Destroy>", lambda event: picker.destroy()
                         if event.widget is win and picker is not None and picker.winfo_exists() else None, add="+")
            action_buttons.append(ResourceButton(actions, text="匹配已收录游戏…", command=bind, width=170, height=38,
                fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED))
            if not item.get("game_id"):
                def new():
                    entry["item"].pop("provider_metadata", None)
                    entry["item"]["game_id"] = None
                    entry["item"]["new"] = True
                    var.set(True)
                    note.configure(text="将添加为自建游戏")
                action_buttons.append(ResourceButton(actions, text="添加为自建游戏", command=new, width=150, height=38,
                    fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED))
            actions.set_items(action_buttons)
            executables = item.get("executables", [])
            if executables:
                choices = ["稍后选择"] + list(executables)
                menu = ctk.CTkOptionMenu(row, **_option_menu_style(), values=choices,
                    command=lambda value: entry.update(exe=None if value == "稍后选择" else value))
                menu.set(choices[1] if len(executables) == 1 else choices[0])
                entry["exe"] = executables[0] if len(executables) == 1 else None
                menu.pack(fill="x", padx=10, pady=(0, 8))
        # Avoid one long widget-creation burst for a large scanned library.
        from slg_motion import MotionScheduler, TimeBudget
        scheduler = MotionScheduler(body)
        index = [0]
        def batch():
            budget = TimeBudget(milliseconds=6, max_items=4)
            while index[0] < len(items) and budget.available:
                build_item(items[index[0]])
                index[0] += 1
                budget.consumed()
            if index[0] < len(items):
                scheduler.call_later(1, batch)
        batch()
        def apply():
            if identity != self._library_identity() or not self._require_personal_access("导入本地游戏"):
                return
            selected = [s for s in states if s["var"].get()]
            if index[0] < len(items):
                status.configure(text="请等待识别列表显示完成。")
                return
            if any(not s["item"].get("game_id") and not s["item"].get("new") for s in selected):
                status.configure(text="请为所选条目选择绑定游戏，或选择添加为自建游戏。")
                return
            ids = [s["item"]["game_id"] for s in selected if s["item"].get("game_id") is not None]
            if len(ids) != len(set(ids)):
                status.configure(text="同一游戏不能同时绑定两个目录，请仅选择一项。")
                return
            source_ids = [(s["item"]["provider_metadata"]["source"], s["item"]["provider_metadata"]["external_id"])
                          for s in selected if s["item"].get("provider_metadata")]
            if len(source_ids) != len(set(source_ids)):
                status.configure(text="同一来源作品不能同时导入两个目录，请仅选择一项。")
                return
            paths = [slg_scan.normalize_path(s["item"]["path"]) for s in selected]
            if len(paths) != len(set(paths)):
                status.configure(text="同一物理目录只能登记一次，请取消重复项。")
                return
            try:
                with slg_db.session() as conn:
                    resolved = []
                    for entry in selected:
                        item = entry["item"]
                        if item.get("provider_metadata"):
                            import slg_library_metadata
                            item["game_id"] = slg_library_metadata.find_existing(conn,item["provider_metadata"]["source"],item["provider_metadata"]["external_id"])
                        if item.get("game_id") is not None:
                            resolved.append(item["game_id"])
                    if len(resolved) != len(set(resolved)):
                        raise ValueError("所选条目现在指向同一作品，请重新确认。")
                    bound = {slg_scan.normalize_path(r["folder_path"]): r["game_id"]
                             for r in conn.execute("SELECT game_id,folder_path FROM local WHERE folder_path IS NOT NULL")}
                    for entry in selected:
                        item = entry["item"]
                        existing = bound.get(slg_scan.normalize_path(item["path"]))
                        if existing is not None and existing != item.get("game_id"):
                            raise ValueError("该目录已登记到其他游戏，请先解除原绑定：" + item["path"])
                    for entry in selected:
                        item = entry["item"]
                        if not os.path.isdir(item["path"]):
                            raise ValueError("目录已不存在：" + item["path"])
                        gid = item.get("game_id")
                        if item.get("provider_metadata"):
                            import slg_library_metadata
                            gid = slg_library_metadata.confirm(conn,item["provider_metadata"],engine=item.get("engine") or None)
                            slg_db.set_local_folder(conn,gid,item["path"],item.get("version"))
                            cover_file=item["provider_metadata"].get("personal_cover_file")
                            if cover_file:
                                library.set_cover_override(conn,gid,cover_file)
                        elif gid is None:
                            gid = slg_db.add_user_game(conn, slg_scan.folder_title(item["name"]),
                                                       folder_path=item["path"], engine=item.get("engine") or None)
                        else:
                            slg_db.set_local_folder(conn, gid, item["path"], item.get("version"))
                        if entry["exe"]:
                            slg_db.set_local_exe(conn, gid, entry["exe"])
                        collection_id = item.get("target_collection_id")
                        if collection_id is not None:
                            if not conn.execute("SELECT 1 FROM collections WHERE id=?",(collection_id,)).fetchone():
                                raise ValueError("扫描方案中的收藏夹已删除，请重新设置方案。")
                            conn.execute("INSERT OR IGNORE INTO collection_items(collection_id,game_id,added_at) VALUES (?,?,?)",
                                         (collection_id,gid,slg_db._now()))
                    conn.commit()
            except Exception as exc:
                status.configure(text="导入失败，未保存：" + str(exc))
                return
            win.destroy()
            self._library_personal_changed()
            self.refresh()
        ResourceButton(win, text="确认导入所选条目", command=apply, fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color_disabled=ON_ACCENT,
                     text_color=ON_ACCENT).pack(fill="x", padx=16, pady=10)
        self._library_scan_dialog = {"win": win, "states": states, "apply": apply, "status": status}
