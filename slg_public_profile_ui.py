"""Explicit, bounded public profile reads; private avatar files never enter here."""
import re
from concurrent.futures import ThreadPoolExecutor
import customtkinter as ctk
import slg_account
from slg_image_resources import ResourceButton, ResourceLabel


def public_projection(response, expected_id):
    value = response.get("profile") if isinstance(response, dict) else None
    if not isinstance(value, dict) or value.get("public_id") != expected_id:
        raise ValueError("服务器返回的公开资料编号不一致")
    visible = value.get("profile_visible")
    if not isinstance(visible, bool):
        raise ValueError("服务器没有返回有效的资料可见性")
    result = dict(public_id=expected_id, nickname=str(value.get("nickname") or "匿名用户")[:40],
                  profile_visible=visible)
    if visible:
        appearance = value.get("equipped_appearances")
        result.update(title_id=str(value.get("equipped_title") or "")[:128],
            appearances={key: str(appearance.get(key) or "")[:128]
                         for key in ("avatar_frame", "comment_frame")} if isinstance(appearance, dict) else {},
            profile_message=str(value.get("profile_message") or "")[:20])
    return result


class PublicProfileUI:
    def _show_public_author_card(self, details):
        import slg_gui as g
        nickname = str(details.get("nickname") or "匿名用户")[:40]
        public_id = details.get("public_id")
        if not isinstance(public_id, str) or not re.fullmatch(r"pp_[0-9a-f]{32}", public_id):
            return self._render_public_author_card(dict(nickname=nickname,
                profile_visible=False, notice="此条旧记录暂无公开资料入口，可刷新后重试。"))
        self._public_profile_generation = getattr(self, "_public_profile_generation", 0) + 1
        generation = self._public_profile_generation
        identity = self._library_identity()
        win = self._new_dialog("公开名片", "460x380")
        label = ResourceLabel(win, text="正在读取公开资料…", text_color=g.TEXT,
                              wraplength=390, font=g.ui_font(size=14))
        label.pack(fill="both", expand=True, padx=20, pady=20)
        retry = ResourceButton(win, text="重试", height=40, fg_color=g.CHIP, text_color=g.TEXT,
            command=lambda: self._show_public_author_card(dict(public_id=public_id, nickname=nickname)))
        ResourceButton(win, text="关闭", height=40, fg_color=g.CHIP, text_color=g.TEXT,
                       command=win.destroy).pack(side="bottom", fill="x", padx=20, pady=16)
        def complete(value, error):
            if (not win.winfo_exists() or generation != self._public_profile_generation
                    or identity != self._library_identity()):
                return
            if error:
                label.configure(text="资料读取失败；没有显示旧快照。\n" + str(error))
                retry.pack(side="bottom", fill="x", padx=20, pady=6)
                return
            try:
                projected = public_projection(value, public_id)
            except ValueError as exc:
                label.configure(text=str(exc))
                retry.pack(side="bottom", fill="x", padx=20, pady=6)
                return
            self._render_public_author_card(projected, window=win)
        def worker():
            try:
                result, error = slg_account.public_profile(public_id), None
            except Exception as exc:
                result, error = None, str(exc)
            self.queue.put(("library_callback", (complete, result, error)))
        executor = getattr(self, "_public_profile_executor", None)
        if executor is None:
            executor = self._public_profile_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="public-profile")
        previous = getattr(self, "_public_profile_future", None)
        if previous is not None:
            previous.cancel()
        self._public_profile_future = executor.submit(worker)
        self._wake_queue()
        return win

    def _build_profile_privacy_controls(self, parent):
        import slg_gui as g
        if not self._has_usable_cloud_session():
            return
        cache = getattr(self, "_cloud_me", {})
        visible = cache.get("profile_visible")
        self._profile_visibility_var = ctk.BooleanVar(value=visible is not False)
        self._profile_visibility_context = self._capture_identity_context()
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(fill="x", padx=18, pady=(4, 14))
        self._profile_visibility_switch = ctk.CTkCheckBox(box, text="公开我的装扮和寄语",
            variable=self._profile_visibility_var, command=self._change_profile_visibility,
            state="normal" if isinstance(visible, bool) and not getattr(self, "_profile_visibility_pending", False) else "disabled",
            fg_color=g.ACCENT, hover_color=g.ACCENT_HOVER, text_color=g.TEXT,
            font=g.ui_font(size=13))
        self._profile_visibility_switch.pack(anchor="w", pady=5)
        self._profile_visibility_status = ResourceLabel(box,
            text=("关闭后，评论及名片仅显示昵称；榜单仍显示排名和分数。"
                  if isinstance(visible, bool) else "同步账号资料后可设置公开范围。"),
            text_color=g.MUTED, font=g.ui_font(size=11), wraplength=430, justify="left")
        self._profile_visibility_status.pack(fill="x", pady=5)

    def _change_profile_visibility(self):
        if getattr(self, "_profile_visibility_pending", False):
            return
        if self._profile_visibility_context != self._capture_identity_context():
            return
        previous = self._cloud_me.get("profile_visible")
        if not isinstance(previous, bool):
            return
        target = bool(self._profile_visibility_var.get())
        self._profile_visibility_var.set(previous)
        self._profile_visibility_pending = True
        self._profile_visibility_switch.configure(state="disabled")
        self._profile_visibility_status.configure(text="正在保存公开范围…")
        started = self._run_cloud_action("profile_visibility",
                    lambda: slg_account.update_profile(profile_visible=target))
        if started is False:
            self._profile_visibility_result(None, "账号操作暂不可用，请重试")

    def _profile_visibility_result(self, result, error):
        self._profile_visibility_pending = False
        value = result.get("profile_visible") if isinstance(result, dict) else None
        if not error and not isinstance(value, bool):
            error = "服务器没有确认可见性，原设置保留"
        status = getattr(self, "_profile_visibility_status", None)
        switch = getattr(self, "_profile_visibility_switch", None)
        if not error:
            self._cloud_me.update(profile_visible=value,
                public_profile_id=result.get("public_profile_id", self._cloud_me.get("public_profile_id")))
            self._public_profile_generation = getattr(self, "_public_profile_generation", 0) + 1
            for child in self.winfo_children():
                if isinstance(child, ctk.CTkToplevel) and child.title() == "公开名片":
                    child.destroy()
            self._comment_response = None
            self._comments_meta = {}
        if switch is not None and switch.winfo_exists():
            self._profile_visibility_var.set(bool(self._cloud_me.get("profile_visible", True)))
            switch.configure(state="normal")
        if status is not None and status.winfo_exists():
            status.configure(text=("保存失败：" + str(error)) if error else
                "已公开装扮和寄语。" if value else "已隐藏装扮和寄语；榜单仍显示排名和分数。")
        self._set_progress("公开范围保存失败，原设置保留" if error else "公开范围已保存")

    def _close_public_profile_reads(self):
        self._public_profile_generation = getattr(self, "_public_profile_generation", 0) + 1
        previous = getattr(self, "_public_profile_future", None)
        if previous is not None:
            previous.cancel()
        self._public_profile_future = None

    def _dispose_public_profiles(self):
        self._close_public_profile_reads()
        executor = getattr(self, "_public_profile_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        self._public_profile_executor = None
