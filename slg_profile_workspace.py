import slg_page_scroll
"""Personal workspace that keeps the underlying catalogue widgets alive."""
import copy
import tkinter as tk
import customtkinter as ctk
from slg_image_resources import ResourceButton, ResourceLabel, ResourceScrollableFrame

FIELDS = ("origin", "catalogue_id", "view", "collection_id", "page", "search", "sort",
          "sort_desc", "include", "exclude", "date_preset", "date_start", "date_end",
          "library_source", "category_ids", "category_uncategorized", "dlsite_languages",
          "dlsite_work_types", "selected")


class ProfileWorkspaceUI:
    def open_profile(self):
        if hasattr(self, '_leave_aux_right_panel'):
            self._leave_aux_right_panel(show_detail=False)
        import slg_gui as g
        current = getattr(self, "_profile_workspace", None)
        if current is not None and current.winfo_exists():
            state = getattr(self, "_profile_return_state", {}) or {}
            if state.get("identity") != self._capture_identity_context():
                self._close_profile_workspace(restore=False)
                return self.open_profile()
            tab = getattr(self, "_profile_tab", "概览")
            if tab in ("个人资料", "账号与设备"):
                self._refresh_local_avatar_profile()
            else:
                self._render_profile_tab(tab)
            return
        self._close_workspace_menu()
        self._leave_collections_view()
        self._close_shop_page()
        self._profile_return_state = {name: copy.deepcopy(getattr(self, name))
                                      for name in FIELDS if hasattr(self, name)}
        self._profile_return_state.update(
            panel=getattr(self, "_panel_mode", "game"), identity=self._capture_identity_context(),
            scroll=self._scroll_offset(), title=self.workspace_title.cget("text"),
            subtitle=self.workspace_subtitle.cget("text"))
        try:
            self._profile_return_state["detail_scroll"] = self.detail._parent_canvas.yview()[0]
        except (AttributeError, tk.TclError):
            pass
        self._profile_hidden_widgets = []
        self.detail.grid_remove()
        self._set_shop_wide_layout(False)
        cached = getattr(self, "_profile_workspace_cached", None)
        if cached is not None and cached.winfo_exists():
            if getattr(self, "_profile_cached_identity", None) == self._capture_identity_context():
                self._profile_workspace = cached
                cached.grid()
                self._panel_mode = "profile"
                stamp = repr((self._account_summary(), getattr(self, "_cloud_me", {})))
                if stamp != getattr(self, "_profile_cached_stamp", None):
                    self._render_profile_tab(getattr(self, "_profile_tab", "概览"))
                slg_page_scroll.begin(self, ("profile", getattr(self, "_profile_tab", "概览")), self._profile_tab_body, force=True)
                slg_page_scroll.finish(self)
                return
            cached.destroy()
        frame = self._profile_workspace = ctk.CTkFrame(self._content_body, fg_color=g.CARD, corner_radius=12)
        frame.grid(row=0, column=1, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)
        self._panel_mode = "profile"
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._make_panel_feedback_header('profile',header,title_size=20).pack(fill='x',padx=12,pady=(12,8))
        ResourceButton(header, text="返回游戏详情", command=self._close_profile_workspace,
                       fg_color=g.CHIP, hover_color=g.CARD_HOVER, text_color=g.TEXT,
                       text_color_disabled=g.MUTED, height=36).pack(side="left")
        tabs = ctk.CTkFrame(frame, fg_color="transparent")
        tabs.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        tabs.grid_columnconfigure((0, 1), weight=1, uniform="profile-tabs")
        self._profile_tabs = {}
        for index, tab in enumerate(("概览", "个人资料", "积分与记录", "账号与设备")):
            button = ResourceButton(tabs, text=tab, height=40, width=1,
                command=lambda value=tab: self._render_profile_tab(value),
                font=g.ui_font(size=13), fg_color=g.CHIP, hover_color=g.CARD_HOVER,
                text_color=g.TEXT, text_color_disabled=g.MUTED)
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=3, pady=3)
            self._profile_tabs[tab] = button
        self._profile_tab_body = ResourceScrollableFrame(frame, fg_color="transparent")
        self._profile_tab_body.grid(row=2, column=0, sticky="nsew")
        self._render_profile_tab(getattr(self, "_profile_tab", "概览"))

    def _render_profile_tab(self, tab):
        import slg_gui as g
        import slg_db
        body = getattr(self, "_profile_tab_body", None)
        if body is None or not body.winfo_exists():
            return
        same_page = getattr(self, "_scroll_page_key", None) == ("profile", tab)
        saved_scroll = slg_page_scroll.snapshot(self, body._parent_canvas) if same_page else None
        if saved_scroll is not None:
            self.after_idle(lambda: slg_page_scroll.restore(self, body._parent_canvas, saved_scroll)
                            if body.winfo_exists() else None)
        previous_tab = getattr(self, "_profile_tab", None)
        self._profile_tab = tab
        if previous_tab != tab or getattr(self, "_scroll_page_key", None) != ("profile", tab):
            slg_page_scroll.begin(self, ("profile", tab), body)
            slg_page_scroll.finish(self)
        self._profile_cached_stamp = repr((self._account_summary(), getattr(self, "_cloud_me", {})))
        for name, button in self._profile_tabs.items():
            button.configure(fg_color=g._mix(g.CHIP, g.ACCENT, 0.20) if name == tab else g.CHIP)
        for child in body.winfo_children():
            child.destroy()
        self._profile_balance_label = self._cloud_account_label = None
        if tab == "概览":
            summary = self._account_summary()
            self._own_avatar_header(body)
            self._own_avatar_head_label.pack_configure(anchor="center")
            ResourceLabel(body, text=summary.nickname, text_color=g.TEXT,
                          font=g.ui_font(size=22, weight="bold")).pack(pady=4)
            balance, color = self._profile_balance_display()
            if not self._has_usable_cloud_session() and not self._is_admin_mode():
                balance, color = "登录后查看云端积分", g.MUTED
            self._profile_balance_label = ResourceLabel(body, text=balance, text_color=color,
                                                        font=g.ui_font(size=16, weight="bold"))
            self._profile_balance_label.pack(pady=6)
            self._profile_section(body, "常用操作")
            actions = ctk.CTkFrame(body, fg_color="transparent")
            actions.pack(fill="x", padx=16, pady=8)
            for column in range(2):
                actions.grid_columnconfigure(column, weight=1, uniform="profile-actions")
            for i, (text, fn) in enumerate((("每日签到", self._on_signin_click),
                    ("我的装扮", self.open_wardrobe), ("兑换与邀请", self.open_redeem),
                    ("积分商城", self.open_shop))):
                ResourceButton(actions, text=text, height=40, fg_color=g.CHIP,
                    hover_color=g.CARD_HOVER, text_color=g.TEXT, text_color_disabled=g.MUTED,
                    command=lambda callback=fn: self._profile_action(callback)).grid(
                    row=i // 2, column=i % 2, sticky="ew", padx=4, pady=4)
            ResourceLabel(body, text="本机头像不上传；积分及装扮以当前账号已同步资料为准。",
                          text_color=g.MUTED, font=g.ui_font(size=12), wraplength=260).pack(pady=12)
            if not self._has_usable_cloud_session() and not self._is_admin_mode():
                ResourceButton(body, text="登录／创建账号", command=self.open_cloud_account,
                    fg_color=g.ACCENT, hover_color=g.ACCENT_HOVER, text_color=g.ON_ACCENT).pack(pady=8)
        elif tab == "个人资料":
            self._own_avatar_header(body)
            self._local_avatar_controls(body)
            ResourceButton(body, text="修改昵称", command=self._edit_nickname,
                fg_color=g.CHIP, hover_color=g.CARD_HOVER, text_color=g.TEXT).pack(fill="x", padx=24, pady=8)
            self._build_profile_privacy_controls(body)
            ResourceButton(body, text="简介与个人装扮…", command=self.open_wardrobe,
                fg_color=g.CHIP, hover_color=g.CARD_HOVER, text_color=g.TEXT).pack(fill="x", padx=24, pady=8)
        elif tab == "积分与记录":
            if self._has_usable_cloud_session() or self._is_admin_mode():
                self._profile_calendar(body)
            else:
                ResourceLabel(body, text="登录后查看账号签到与积分记录。", text_color=g.MUTED).pack(pady=16)
            self._profile_section(body, "本机共用数据（不随账号同步）")
            self._profile_cells(body, (("收藏", slg_db.collection_count(self.conn)),
                                     ("评分", slg_db.rating_count(self.conn)),
                                     ("本地目录", slg_db.local_count(self.conn))))
            self._profile_lottery_log(body)
        else:
            self._profile_identity_switch(body, admin=self._is_admin_mode())
            if self._is_admin_mode():
                self._render_admin_profile(body)
            else:
                self._profile_cloud_account(body, fetch=False)

    def _profile_action(self, callback):
        if getattr(callback, "__name__", "") == "open_shop":
            self._close_profile_workspace()
        if getattr(callback, '__name__', '') == 'open_shop':
            self._open_panel_entry('shop',callback)
        else:
            callback()

    def _close_profile_workspace(self, restore=True, render_detail=True):
        frame = getattr(self, "_profile_workspace", None)
        if frame is None:
            return
        self._deactivate_panel_feedback('profile')
        self._profile_workspace = None
        state = getattr(self, "_profile_return_state", {}) or {}
        if frame.winfo_exists():
            if restore and state.get("identity") == self._capture_identity_context():
                frame.grid_remove()
                self._profile_workspace_cached = frame
                self._profile_cached_identity = state.get("identity")
            else:
                frame.destroy()
                self._profile_workspace_cached = None
        self._profile_return_state = None
        self._profile_hidden_widgets = []
        self._panel_mode = "game"
        if getattr(self, "detail", None) is not None and self.detail.winfo_exists():
            self.detail.grid()
        # The catalogue remains interactive: never roll back its current filters,
        # page, selection or scroll to the snapshot from opening this pane.
        if restore and render_detail:
            game = getattr(self, "selected", None)
            slg_page_scroll.begin(self, ("game", getattr(self, "catalogue_id", None), game["id"] if game else None),
                                  self.detail, offset=state.get("detail_scroll", 0.0), force=True)
            slg_page_scroll.finish(self)
            self._render_detail()
