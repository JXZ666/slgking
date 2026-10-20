"""Identity guarded collection workspace. Workers never touch Tk or write SQLite."""
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
import slg_collections_workspace as model
from slg_image_resources import ResourceButton, ResourceLabel, ResourceScrollableFrame


class CollectionsUI:
    def capture_collections_workspace(self):
        return dict(getattr(self, "_collections_spec", {}), page=getattr(self, "_collections_page", 1),
                    overview_page=getattr(self, "_collections_overview_page", 1))

    def leave_collections_workspace(self):
        self._collections_generation = getattr(self, "_collections_generation", 0) + 1
        future = getattr(self, "_collections_future", None)
        if future is not None:
            future.cancel()
        frame = getattr(self, "_collections_frame", None)
        if frame is not None:
            frame.destroy()
        self._collections_frame = None

    def open_collections_workspace(self, collection_id=None, state=None):
        import slg_gui as gui
        self.leave_collections_workspace()
        self._workspace_mode = "collections"
        self._collections_identity = self._library_identity()
        self._collections_spec = dict(state or getattr(self, "_collections_spec", {}))
        self._collections_spec.setdefault("source", "all")
        self._collections_spec.setdefault("sort", "title")
        self._collections_spec.setdefault("desc", False)
        self._collections_spec.setdefault("date_preset", "all")
        self._collections_spec["collection_id"] = collection_id
        self._collections_page = (state or {}).get("page", 1)
        self._collections_overview_page = (state or {}).get("overview_page", getattr(self, "_collections_overview_page", 1))
        color = lambda name: (gui._LIGHT[name], gui._DARK[name])
        frame = self._collections_frame = ctk.CTkFrame(self._content_body, fg_color=color("BG"))
        frame.grid(row=0, column=0, rowspan=2, columnspan=2 if collection_id is None else 1, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(4, weight=1)
        self._collections_title = ResourceLabel(frame, text="收藏夹", text_color=color("TEXT"), font=gui.ui_font(size=20))
        self._collections_title.grid(row=0, column=0, sticky="ew", padx=16, pady=10)
        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.grid(row=1, column=0, padx=12)
        common = dict(text_color=color("TEXT"), text_color_disabled=color("MUTED"), fg_color=color("CHIP"), hover_color=color("CARD_HOVER"))
        if collection_id is None:
            ResourceButton(actions, text="新建收藏夹…", command=lambda: self._collections_edit("create"), **common).pack(side="left", padx=4)
        else:
            for label, command in [("返回总览", lambda: self.open_collections_workspace()),
                                   ("重命名…", lambda: self._collections_edit("rename")),
                                   ("删除收藏夹…", lambda: self._collections_edit("delete"))]:
                ResourceButton(actions, text=label, command=command, width=95, **common).pack(side="left", padx=3)
            self._collections_controls(frame, color)
        self._collections_status = ResourceLabel(frame, text="读取收藏夹…", text_color=color("MUTED"))
        self._collections_status.grid(row=3, column=0, sticky="w", padx=16, pady=8)
        self._collections_list = ResourceScrollableFrame(frame, fg_color=color("BG"))
        self._collections_rows = []
        self._collections_list.grid(row=4, column=0, sticky="nsew", padx=10, pady=6)
        self._collections_pager = ctk.CTkFrame(frame, fg_color="transparent")
        self._collections_pager.grid(row=5, column=0, sticky="ew", padx=14, pady=8)
        self._collections_prev = ResourceButton(self._collections_pager, text="上一页", width=80, command=lambda:self._collections_turn(-1), **common)
        self._collections_prev.pack(side="left")
        self._collections_page_label = ResourceLabel(self._collections_pager, text="", text_color=color("TEXT"))
        self._collections_page_label.pack(side="left", padx=12)
        self._collections_next = ResourceButton(self._collections_pager, text="下一页", width=80, command=lambda:self._collections_turn(1), **common)
        self._collections_next.pack(side="left")
        self.refresh_collections_workspace()

    def _collections_controls(self, frame, color):
        # Filters wrap into two rows instead of growing one long toolbar.
        bar = ctk.CTkFrame(frame, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 0))
        bar.grid_columnconfigure(0, weight=1)
        entry = ctk.CTkEntry(bar, placeholder_text="搜索此收藏夹", justify="center", text_color=color("TEXT"))
        entry.grid(row=0, column=0, sticky="ew", padx=3, pady=3)
        entry.insert(0, self._collections_spec.get("search", ""))
        def search(event=None):
            self._collections_spec["search"] = entry.get().strip()
            self._collections_page = 1
            self.refresh_collections_workspace()
        entry.bind("<Return>", search)
        ResourceButton(bar, text="搜索", width=60, command=search, text_color=color("TEXT"), text_color_disabled=color("MUTED"), hover_color=color("CARD_HOVER"), fg_color=color("CHIP")).grid(row=0, column=1, padx=3)
        groups = [("source", {"全部来源":"all", "SLG大类":"western_slg", "DLsite":"dlsite", "自建游戏":"user"}),
                  ("date_preset", {"全部日期":"all", "近7天":"7", "近30天":"30", "近90天":"90", "自定义日期…":"custom"}),
                  ("sort", {"名称":"title", "最近更新":"updated", "按xp推荐":"score", "DLsite评分":"source_rating"})]
        menus = ctk.CTkFrame(bar, fg_color="transparent")
        menus.grid(row=1, column=0, columnspan=2)
        self._collections_menus = {}
        for key, options in groups:
            def choose(label, key=key, options=options):
                value = options[label]
                if value == "custom":
                    identity = self._collections_identity
                    start = simpledialog.askstring("起始日期", "请输入YYYY-MM-DD：", parent=self)
                    if start is None or identity != self._library_identity(): return
                    end = simpledialog.askstring("结束日期", "请输入YYYY-MM-DD：", parent=self)
                    if end is None or identity != self._library_identity(): return
                    from slg_date_filters import resolve_date_range
                    try:
                        resolve_date_range("custom", start, end)
                    except ValueError as exc:
                        messagebox.showerror("日期无效", str(exc), parent=self); return
                    self._collections_spec.update(date_start=start, date_end=end)
                self._collections_spec[key] = value
                if key == "source":
                    incompatible = (value == "dlsite" and self._collections_spec.get("sort") == "score") or (value != "dlsite" and self._collections_spec.get("sort") == "source_rating")
                    if incompatible:
                        self._collections_spec["sort"] = "title"
                        self._collections_spec["desc"] = False
                        self._collections_menus["sort"].set("名称")
                    self._collections_menus["sort"].configure(values=["名称", "最近更新", "DLsite评分" if value == "dlsite" else "按xp推荐"])
                if key == "sort" and value == "score" and self._collections_spec.get("source") == "dlsite":
                    self._collections_spec["sort"] = "title"
                    messagebox.showinfo("排序说明", "DLsite保留原生评分，不参与xp推荐。", parent=self)
                if key == "sort": self._collections_spec["desc"] = value != "title"
                self._collections_page = 1
                self.refresh_collections_workspace()
            menu = ctk.CTkOptionMenu(menus, values=list(options), command=choose, width=118, text_color=color("TEXT"), fg_color=color("CHIP"), button_color=color("CHIP"))
            menu.pack(side="left", padx=3, pady=3)
            self._collections_menus[key] = menu
            if key == "sort": menu.configure(values=["名称", "最近更新", "DLsite评分" if self._collections_spec.get("source") == "dlsite" else "按xp推荐"])
            menu.set(next((label for label, value in options.items() if value == self._collections_spec.get(key)), next(iter(options))))

    def refresh_collections_workspace(self):
        if getattr(self, "_workspace_mode", None) != "collections" or not getattr(self, "_collections_frame", None): return
        self._collections_generation += 1
        getattr(self, "_navigation_query_started", lambda: None)()
        generation = self._collections_generation
        self._collections_indicator_generation = generation
        identity = self._library_identity()
        stamp = self._catalogue_data_stamp() if hasattr(self, '_catalogue_data_stamp') else None
        spec, page = dict(self._collections_spec), self._collections_page
        db_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
        def show_loading():
            if (generation == self._collections_generation
                    and generation == self._collections_indicator_generation
                    and getattr(self, '_workspace_mode', None) == 'collections'):
                self._collections_status.configure(text="正在读取，原列表暂时保留…")
        self.after(120, show_loading)
        def worker():
            try: value, error = model.read_snapshot(db_path, spec, page), None
            except Exception as exc: value, error = None, str(exc)
            self.queue.put(("library_callback", (finish, (value, error), None)))
        def finish(payload, ignored):
            if generation != self._collections_generation or identity != self._library_identity() or getattr(self, "_workspace_mode", None) != "collections": return
            value, error = payload
            if stamp is not None and stamp != self._catalogue_data_stamp():
                self.refresh_collections_workspace()
                return
            if error:
                getattr(self, "_navigation_query_completed", lambda **kw: False)(error=True)
                self._collections_indicator_generation = None
                self._collections_status.configure(text="读取失败：" + error)
                return
            self._collections_render(*value)
            getattr(self, "_navigation_query_completed", lambda: False)()
            # Rendering may navigate to a new page if the folder disappeared.
            # Never invalidate the new read while ending the old indicator.
            if generation == self._collections_generation:
                self._collections_indicator_generation = None
        future = getattr(self, "_collections_future", None)
        if future is not None:
            future.cancel()
        self._collections_future = self._query_executor.submit(worker)
        if hasattr(self, '_wake_queue'):
            self._wake_queue()

    def _collections_render(self, collections, result):
        import slg_gui as gui
        color = lambda name: (gui._LIGHT[name], gui._DARK[name])
        common = dict(text_color=color("TEXT"), text_color_disabled=color("MUTED"), fg_color=color("CHIP"), hover_color=color("CARD_HOVER"))
        selected = next((row for row in collections if row["id"] == self._collections_spec["collection_id"]), None)
        self._navigation_collection_games = list(result.rows) if result is not None else []
        self._navigation_collection_total = result.total if result is not None else len(collections)
        if result is None:
            self._collections_status.configure(text="自定义收藏夹与“收藏”状态分别管理。请选择收藏夹查看成员。" if collections else "尚无收藏夹。新建后可从游戏详情加入游戏。")
            pages = max(1, (len(collections) + 11)//12)
            page = self._collections_overview_page = max(1, min(self._collections_overview_page, pages))
            visible_rows = collections[(page-1)*12:page*12]
            for index, item in enumerate(visible_rows):
                slot = self._collections_slot(index, color)
                text = "%s    %d款 · 当前可见%d款" % (item["name"], item["total_count"], item["visible_count"])
                signature = ("overview", item["id"], text)
                if slot._signature != signature:
                    slot._title.configure(text=text, command=lambda cid=item["id"]: self.open_collections_workspace(cid))
                    slot._signature = signature
                self._collections_toggle(slot._cover, False)
                self._collections_toggle(slot._remove, False)
                self._collections_toggle(slot._launch, False)
                self._collections_toggle(slot, True, fill="x", padx=5, pady=5)
            self._collections_trim_rows(len(visible_rows))
            self._collections_update_pager(page, pages)
            return
        if selected is None:
            self.open_collections_workspace(); return
        self._collections_title.configure(text=selected["name"])
        self._collections_status.configure(text="总成员%d · 当前可见%d · 筛选结果%d（隐藏作品不显示）" % (selected["total_count"], selected["visible_count"], result.total))
        self._collections_page = result.page
        for index, game in enumerate(result.rows):
            row = self._collections_slot(index, color)
            source = "自建" if game.get("origin") == "user" else "DLsite" if "dlsite" in game.get("catalog_ids", ()) else "SLG大类"
            text = game["title"] + "\n" + source + (" · 已绑定本地目录" if game.get("folder_path") else " · 未绑定")
            signature = ("member", game["id"], text, game.get("exe_path"))
            if row._signature != signature:
                row._title.configure(text=text, command=lambda game=game:self.select(game))
                row._launch.configure(command=lambda game=game:self._launch_game(game))
                row._remove.configure(command=lambda gid=game["id"]:self._collections_edit("remove", gid))
                row._signature = signature
            else:
                # Fresh facts can change detail content without visible card text changes.
                row._title.configure(command=lambda game=game:self.select(game))
                row._launch.configure(command=lambda game=game:self._launch_game(game))
            cover_signature = (game["slug"], game.get("cover_file"), bool(game.get("has_cover_override")), bool(game.get("cover_override_missing")))
            if row._cover_signature != cover_signature:
                self._bind_cover(row._cover, game, width=84, height=52)
                row._cover_signature = cover_signature
            self._collections_toggle(row._cover, True, side="left", padx=5, before=row._title)
            self._collections_toggle(row._launch, bool(game.get("exe_path")), side="right", padx=3)
            self._collections_toggle(row._remove, True, side="right", padx=3)
            self._collections_toggle(row, True, fill="x", padx=5, pady=5)
        self._collections_trim_rows(len(result.rows))
        if not result.rows: self._collections_status.configure(text="暂无符合条件的游戏；总成员%d · 当前可见%d，可调整筛选。" % (selected["total_count"], selected["visible_count"]))
        self._collections_update_pager(result.page, max(1, (result.total+7)//8))

    @staticmethod
    def _collections_toggle(widget, visible, **pack_options):
        mapped = bool(widget.winfo_manager())
        if visible and not mapped: widget.pack(**pack_options)
        elif not visible and mapped: widget.pack_forget()

    def _collections_trim_rows(self, count):
        for row in self._collections_rows[count:]:
            self._collections_toggle(row, False)

    def _collections_update_pager(self, page, pages):
        self._collections_prev.configure(state="normal" if page > 1 else "disabled")
        self._collections_next.configure(state="normal" if page < pages else "disabled")
        self._collections_page_label.configure(text="%d / %d" % (page, pages))

    def _collections_turn(self, delta):
        if self._collections_spec.get("collection_id") is None:
            self._collections_overview_page += delta
        else:
            self._collections_page += delta
        self.refresh_collections_workspace()

    def _collections_slot(self, index, color):
        while len(self._collections_rows) <= index:
            row = ctk.CTkFrame(self._collections_list, fg_color=color("CARD"))
            common = dict(text_color=color("TEXT"), text_color_disabled=color("MUTED"), fg_color=color("CHIP"), hover_color=color("CARD_HOVER"))
            row._signature = row._cover_signature = None
            row._cover = ResourceLabel(row, text="", width=84, height=52)
            row._title = ResourceButton(row, text="", anchor="w", **common)
            row._title.pack(side="left", fill="x", expand=True, padx=5, pady=5)
            row._launch = ResourceButton(row, text="启动", width=55, **common)
            row._remove = ResourceButton(row, text="移出", width=55, **common)
            self._collections_rows.append(row)
        return self._collections_rows[index]

    def _collections_edit(self, operation, game_id=None):
        identity = self._collections_identity
        if identity != self._library_identity() or not self._require_personal_access("管理收藏夹"):
            return
        cid = self._collections_spec.get("collection_id")
        def apply(name=None):
            if identity != self._library_identity():
                return False
            if operation == "create": model.create_collection(self.conn, name)
            elif operation == "rename": model.rename_collection(self.conn, cid, name)
            elif operation == "delete": model.delete_collection(self.conn, cid)
            elif operation == "remove": model.remove_member(self.conn, cid, game_id)
            self._library_personal_changed()
            if getattr(self, "_workspace_mode", None) == "collections":
                if operation == "delete": self.open_collections_workspace()
                else: self.refresh_collections_workspace()
            return True
        if operation in ("create", "rename"):
            from slg_text_input import open_text_input
            current = self.conn.execute("SELECT name FROM collections WHERE id=?", (cid,)).fetchone() if cid else None
            return open_text_input(self, "新建收藏夹" if operation == "create" else "重命名收藏夹",
                "请输入收藏夹名称", current[0] if current else "", apply)
        if not messagebox.askyesno("确认操作", "只移除收藏关系，不删除游戏或磁盘文件。继续吗？", parent=self):
            return
        try:
            apply()
        except Exception as exc:
            messagebox.showerror("操作未完成", str(exc), parent=self)
