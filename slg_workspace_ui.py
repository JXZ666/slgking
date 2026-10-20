"""Workspace chrome and explicit local-library actions; all methods run on Tk."""
import tkinter as tk

import customtkinter as ctk


def palette():
    # Late import keeps the current theme and avoids a GUI import cycle.
    import slg_gui
    return slg_gui


class WorkspaceUI:
    def _apply_workspace_density(self, profile):
        if not hasattr(self, "workspace_subtitle"):
            return
        compact = profile == "compact"
        if compact:
            self.workspace_subtitle.grid_remove()
        else:
            self.workspace_subtitle.grid()

    def _request_catalogue_page(self, preserve_scroll=False, skip_if_same=False):
        from slg_catalogue_query import query_catalogue_page, open_readonly
        getattr(self, "_navigation_query_started", lambda: None)()
        self._catalogue_query_generation = getattr(self, "_catalogue_query_generation", 0) + 1
        generation = self._catalogue_query_generation
        identity = self._library_identity()
        stamp = self._catalogue_data_stamp()
        spec = dict(origin=self.origin, catalog_id=self.catalogue_id,
            search=self.search or None, include=tuple(self.include), exclude=tuple(self.exclude),
            statuses=(self.view,) if self.view else (), collection_id=self.collection_id,
            sort=self.sort, desc=self.sort_desc, date_preset=self.date_preset,
            date_start=self.date_start, date_end=self.date_end,
            dlsite_languages=tuple(self.dlsite_languages),
            dlsite_work_types=tuple(self.dlsite_work_types))
        if self.origin == "user":
            spec.update(catalog_id=None, category_ids=tuple(self.category_ids),
                        uncategorized=self.category_uncategorized)
        page, size = self.page, palette().PAGE_SIZE
        focus = getattr(self, "_catalogue_focus_gid", None)
        self._catalogue_focus_gid = None
        if focus is not None:
            spec["focus_id"] = focus
        database = self.conn.execute("PRAGMA database_list").fetchone()[2]
        offset = self._scroll_offset() if preserve_scroll else None
        previous = getattr(self, "_catalogue_query_future", None)
        if previous is not None:
            previous.cancel()
        self._catalogue_query_pending = True
        self._catalogue_paged = True
        self._card_render_pending = True
        def show_loading():
            label = getattr(self, "_catalogue_status_label", None)
            if (generation == self._catalogue_query_generation and self._catalogue_query_pending
                    and label is not None and label.winfo_exists()):
                label.configure(text="正在筛选，原列表暂时保留…")
        # Avoid flashing an indicator for a fast cached query.
        self.after(120, show_loading)
        def complete(value, error):
            if (generation != self._catalogue_query_generation
                    or identity != self._library_identity()
                    or self.origin == "local"
                    or getattr(self, "_workspace_mode", None) == "collections"):
                return
            self._catalogue_query_pending = False
            if stamp != self._catalogue_data_stamp():
                # A committed rating/path/cover/sync change supersedes the
                # snapshot; do not paint stale personal data over the new value.
                self._catalogue_focus_gid = focus
                self._request_catalogue_page(preserve_scroll, skip_if_same)
                return
            if error:
                getattr(self, "_navigation_query_completed", lambda **kw: False)(error=True)
                self._card_render_pending = False
                self.page = getattr(self, "_catalogue_rendered_page", self.page)
                self._render_pager()
                self._log("目录读取失败：" + str(error))
                self._set_progress("目录读取失败，可调整筛选后重试")
                self._catalogue_status_label.configure(text="读取失败；原列表保留，请重试")
                return
            self.rows, self._catalogue_total, self.page = value.rows, value.total, value.page
            self._catalogue_rendered_page = value.page
            count_text = "筛选结果 %d 款" % value.total
            if self._catalogue_status_label.cget("text") != count_text:
                self._catalogue_status_label.configure(text=count_text)
            self._catalogue_last_query_timings = dict(value.timings)
            if self.selected:
                self.selected = next((dict(g) for g in self.rows
                    if g["id"] == self.selected["id"]), self.selected)
            visible = [g["id"] for g in self.rows]
            # _sync_cards itself compares content, not merely IDs; a changed
            # version/cover on the same page must repaint its affected slot.
            self._sync_cards(self.rows)
            self._render_pager()
            self._set_empty_label(not self.rows)
            self._rendered_ids = visible
            if offset is not None:
                self._restore_scroll(offset)
            self._render_stats()
            self._refresh_wishlist_notice()
            navigation_handled = getattr(self, "_navigation_query_completed", lambda: False)()
            if not navigation_handled and focus is None:
                self._render_detail_if_stale()
            if not navigation_handled and focus is not None:
                self.selected = next((dict(g) for g in self.rows if g["id"] == focus), None)
                if self.selected is not None:
                    self.select(self.selected)
            edge = getattr(self, "_catalogue_selection_edge", None)
            self._catalogue_selection_edge = None
            if not navigation_handled and edge is not None and self.rows:
                self.select(self.rows[edge])
                self._scroll_card_into_view(self.rows[edge]["id"])
        def query():
            connection = None
            try:
                connection = open_readonly(database)
                value, error = query_catalogue_page(connection, spec, page, size), None
            except Exception as exc:
                value, error = None, exc
            finally:
                if connection is not None:
                    connection.close()
            self.queue.put(("library_callback", (complete, value, error)))
        self._catalogue_query_future = self._query_executor.submit(query)
        self._wake_queue()

    def _leave_collections_view(self):
        if getattr(self, "_workspace_mode", None) != "collections":
            return
        self.leave_collections_workspace()
        self._workspace_mode = "catalogue"
        if getattr(self, "toolbar", None) is not None:
            self.toolbar.grid()
        self._paint_user_view()

    def _build_workspace_toolbar(self, parent):
        g = palette()
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 0))
        bar.grid_columnconfigure(0, weight=1)
        self.toolbar = bar

        self.search_entry = ctk.CTkEntry(bar, placeholder_text="搜索名称、别名或作品编号…",
            height=38, corner_radius=8, fg_color=g.CARD, text_color=g.TEXT,
            placeholder_text_color=g.MUTED, border_width=1, border_color=g.CHIP,
            font=g.ui_font(size=13), justify="center")
        self.search_entry.grid(row=1, column=0, sticky="ew")
        self.search_entry.bind("<KeyRelease>", self._on_search)
        sorting = ctk.CTkFrame(bar, fg_color="transparent")
        sorting.grid(row=1, column=1, sticky="e", padx=(10, 0))
        g.ResourceButton(sorting, text="标签筛选", width=88, height=38,
            fg_color=g.CARD, text_color=g.TEXT, text_color_disabled=g.MUTED,
            hover_color=g.CARD_HOVER, font=g.ui_font(size=13),
            command=self.open_tag_picker).pack(side="left", padx=(0, 8))
        g.ResourceLabel(sorting, text="排序", text_color=g.MUTED,
                        font=g.ui_font(size=11)).pack(side="left", padx=(0, 6))
        self.sort_menu = ctk.CTkOptionMenu(sorting, values=list(g.SORT_FIELDS),
            width=116, height=38, corner_radius=8, fg_color=g.CARD,
            text_color=g.TEXT, button_color=g.CHIP, button_hover_color=g.CARD_HOVER,
            command=self._on_sort)
        self.sort_menu.pack(side="left")
        self.sort_dir_btn = g.ResourceButton(sorting, text=g.SORT_ARROW[self.sort_desc],
            width=64, height=38, corner_radius=8, fg_color=g.CARD, text_color=g.TEXT,
            text_color_disabled=g.MUTED, hover_color=g.CARD_HOVER,
            font=g.ui_font(size=13), command=self._toggle_sort_dir)
        self.sort_dir_btn.pack(side="left", padx=(6, 0))

        self.catalogue_row = ctk.CTkFrame(bar, fg_color="transparent")
        self.catalogue_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.catalogue_switch = g.PaletteSegmentedButton(self.catalogue_row,
            values=[c["label"] for c in g.CATALOGUES.values()],
            command=lambda label: self.set_catalogue(next(
                k for k, c in g.CATALOGUES.items() if c["label"] == label)),
            font=g.ui_font(size=12), height=30, fg_color=g.CHIP,
            selected_color=g.ACCENT, selected_hover_color=g.ACCENT_HOVER,
            unselected_color=g.CARD, unselected_hover_color=g.CARD_HOVER,
            text_color=g.TEXT, text_color_disabled=g.MUTED)
        self.catalogue_switch.pack(side="left")
        self.catalogue_notice = g.ResourceLabel(self.catalogue_row, text="",
            text_color=g.MUTED, font=g.ui_font(size=11), wraplength=310, anchor="w")
        self.catalogue_notice.pack(side="left", padx=(12, 0))

        # The old permanent notice remains discoverable without a full-width
        # promotional band taking space from the library's game list.
        self.vpn_notice = ctk.CTkFrame(bar, fg_color="transparent")
        self.vpn_notice.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        g.ResourceButton(self.vpn_notice, text="数据来源与网络说明", width=150, height=22,
            fg_color="transparent", text_color=g.MUTED, text_color_disabled=g.MUTED,
            hover_color=g.CARD_HOVER, font=g.ui_font(size=11),
            command=self.open_data_sources).pack(side="left")
        self._catalogue_status_label = g.ResourceLabel(self.vpn_notice, text="",
            text_color=g.MUTED, font=g.ui_font(size=11), anchor="e")
        self._catalogue_status_label.pack(side="right", padx=4)
        self._build_library_actions(bar)
        self._catalogue_controls_sig = None
        self._configure_catalogue_controls()
        self._refresh_announcement_badge()

    def _build_library_actions(self, parent):
        g = palette()
        row = ctk.CTkFrame(parent, fg_color=g.CARD, corner_radius=10)
        row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.library_actions = row
        actions_flow = g.FlowFrame(row, fg_color="transparent", gap_x=6, gap_y=6, centered=True)
        actions_flow.pack(fill="x", padx=10, pady=(10, 4))
        primary = g.ResourceButton(actions_flow, text="扫描并导入…", width=120, height=34,
            fg_color=g.ACCENT, text_color=g.ON_ACCENT, text_color_disabled=g.ON_ACCENT,
            hover_color=g.ACCENT_HOVER, font=g.ui_font(size=12),
            command=self.open_library_import)
        definitions = (
            ("添加游戏 ▾", (("查询资料添加…", lambda: self.open_metadata_query({"name": ""})),
                               ("手动添加未匹配游戏…", lambda: self.open_add_game()))),
            ("扫描管理 ▾", (("扫描路径与方案…", lambda: self.open_library_import()),
                               ("待确认结果与失败重试…", lambda: self.open_scan_review()))),
            ("维护 ▾", (("检查游戏版本…", lambda: self.do_updates()),
                           ("扫描本地存档…", lambda: self.do_scan_saves()),
                           ("查看存档台账…", lambda: self.open_save_inventory()))),
        )
        self.library_action_buttons = [primary]
        self.library_action_definitions = definitions
        for column, (label, actions) in enumerate(definitions, 1):
            button = g.ResourceButton(actions_flow, text=label, width=96, height=34,
                fg_color=g.CHIP, text_color=g.TEXT, text_color_disabled=g.MUTED,
                hover_color=g.CARD_HOVER, font=g.ui_font(size=12))
            button.configure(command=lambda b=button, a=actions: self._workspace_menu(b, a))
            self.library_action_buttons.append(button)
        actions_flow.set_items(self.library_action_buttons)
    def _close_workspace_menu(self):
        menu = getattr(self, "_workspace_popup", None)
        if menu is not None:
            menu.dispose()
        self._workspace_popup = None

    def _workspace_menu(self, button, actions):
        from slg_action_menu import ActionMenu
        g = palette()
        self._close_workspace_menu()
        menu = ActionMenu(self, font=g.ui_font(size=14),
            palette=dict(card=g.CARD, hover=g.CARD_HOVER, text=g.TEXT,
                         muted=g.MUTED, accent=g.ACCENT),
            screen_bounds=self._usable_screen_bounds)
        self._workspace_popup = menu
        def invoke(label, command):
            tool = {"检查游戏版本…": "check_updates", "扫描本地存档…": "scan_local_saves",
                    "查看存档台账…": "scan_local_saves"}.get(label)
            if tool:
                self._report_ui_event("tool_open", tool)
            command()
        menu.open(button, [(label, lambda l=label, c=command: invoke(l, c),
                    lambda l=label: not self.busy or l == "查看存档台账…")
                    for label, command in actions])

    def _update_workspace_chrome(self):
        self._close_workspace_menu()
        local = self.origin in ("local", "user")
        want = not local and self.view == "want"
        title = "本地游戏库" if local else "想玩" if want else "全部游戏库"
        if local:
            subtitle = "管理电脑上的游戏：扫描、确认、启动与维护"
        elif want:
            count = len(getattr(self, "_wishlist_updates", ()))
            subtitle = ("想玩的作品中，有 %d 款出现版本更新" % count if count else
                        "整理想玩的作品，关注版本更新")
        else:
            subtitle = "筛选作品范围，再按你的偏好排序"
        self.workspace_title.configure(text=title)
        self.workspace_subtitle.configure(text=subtitle)
        if local:
            self.library_actions.grid()
            self.catalogue_row.grid_remove()
            self.vpn_notice.grid_remove()
        else:
            self.library_actions.grid_remove()
            self.catalogue_row.grid()
            self.vpn_notice.grid()

    def open_date_filter(self):
        g = palette()
        from slg_date_filters import resolve_date_range
        dialog = self._new_dialog("游戏更新日期", "440x350")
        labels = {"全部日期": "all", "近7天": "7", "近30天": "30",
                  "近90天": "90", "自定义": "custom"}
        initial = getattr(self, "date_preset", "all")
        choice = ctk.StringVar(value=next((k for k, v in labels.items() if v == initial), "全部日期"))
        g.ResourceLabel(dialog, text="限定更新日期后，仍可按xp推荐等方式排序",
            text_color=g.TEXT, font=g.ui_font(size=12), wraplength=390).pack(padx=18, pady=(18, 12))
        selector = ctk.CTkOptionMenu(dialog, values=list(labels), variable=choice,
            fg_color=g.CHIP, text_color=g.TEXT, button_color=g.CHIP,
            button_hover_color=g.CARD_HOVER)
        selector.pack(fill="x", padx=18)
        entries = []
        for label, key in (("起始日期（YYYY-MM-DD）", "date_start"), ("结束日期（YYYY-MM-DD）", "date_end")):
            g.ResourceLabel(dialog, text=label, text_color=g.MUTED,
                            font=g.ui_font(size=11), anchor="w").pack(fill="x", padx=18, pady=(10, 3))
            entry = ctk.CTkEntry(dialog, height=30, fg_color=g.CARD, text_color=g.TEXT,
                                border_color=g.CHIP)
            entry.insert(0, getattr(self, key, None) or "")
            entry.pack(fill="x", padx=18)
            entries.append(entry)
        feedback = g.ResourceLabel(dialog, text="未知更新日期的作品不匹配限定范围。",
            text_color=g.MUTED, font=g.ui_font(size=11))
        feedback.pack(padx=18, pady=10)
        def enable_custom(label):
            for entry in entries:
                entry.configure(state="normal" if labels[label] == "custom" else "disabled")
        selector.configure(command=enable_custom)
        enable_custom(choice.get())
        def apply():
            preset = labels[choice.get()]
            try:
                start, end = resolve_date_range(preset, entries[0].get().strip() or None,
                                               entries[1].get().strip() or None)
            except ValueError as exc:
                feedback.configure(text=str(exc), text_color=g.DANGER_TEXT)
                return
            self.date_preset, self.date_start, self.date_end = preset, start, end
            self.page = 1
            dialog.destroy()
            self.refresh()
        g.ResourceButton(dialog, text="应用日期筛选", command=apply, height=34,
            fg_color=g.ACCENT, text_color=g.ON_ACCENT, text_color_disabled=g.ON_ACCENT,
            hover_color=g.ACCENT_HOVER).pack(fill="x", padx=18, pady=(0, 14))

    def _clear_date_filter(self):
        self.date_preset, self.date_start, self.date_end = "all", None, None
        self.page = 1
        self.refresh()
