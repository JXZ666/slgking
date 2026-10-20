"""Click-only notification center. Bell redness is reward state, never unread state."""
import customtkinter as ctk
import tkinter as tk
import json
from slg_account_chip import _AccountPopup
from slg_image_resources import ResourceButton, ResourceLabel
from slg_reply_ui import CommunityTasks
import slg_comment_replies as api


def reward_dot(projection, logged_in):
    """Projection is account/channel validated by the owner before this call."""
    return bool(logged_in and isinstance(projection, dict) and projection.get('claimable') is True)


class _BellButton(ResourceButton):
    """Code-drawn monochrome icon; no emoji font or platform glyph dependency."""
    def __init__(self, *args, **kwargs):
        self._bell_dot = False
        super().__init__(*args, text='', width=44, height=44, **kwargs)
        self._canvas.configure(takefocus=1)
        for sequence in ('<Return>', '<space>'):
            self._canvas.bind(sequence, lambda event: (self.invoke(), 'break')[-1], add='+')
        self._draw()

    def set_dot(self, value):
        self._bell_dot = value is True
        self._draw()

    def _draw(self, no_color_updates=False):
        super()._draw(no_color_updates)
        if not hasattr(self, '_canvas'): return
        canvas = self._canvas
        canvas.delete('notification_icon')
        # CTk truncates scaled *integers*. Scaling 1 at 150% returned 1,
        # centering a 44px drawing in a 66px button. Preserve the exact ratio;
        # current dimensions are logical and already follow resize events.
        scale = self._apply_widget_scaling(1.0)
        cx, cy = self._current_width * scale / 2, self._current_height * scale / 2
        color = self._apply_appearance_mode(self.cget(
            'text_color_disabled' if self.cget('state') == 'disabled' else 'text_color'))
        def coords(values):
            return [cx + values[i] * scale if i % 2 == 0 else cy + values[i] * scale for i in range(len(values))]
        canvas.create_line(*coords([-10,7,-8,3,-8,-3,-6,-9,0,-12,6,-9,8,-3,8,3,10,7,-10,7]),
            fill=color, width=1.8*scale, smooth=True, tags=('notification_icon', 'notification_glyph'))
        canvas.create_arc(*coords([-3,7,3,13]), start=180, extent=180, style='arc', outline=color, width=1.8*scale, tags=('notification_icon', 'notification_glyph'))
        if self._bell_dot:
            canvas.create_oval(*coords([9,-15,16,-8]), fill='#ef4444', outline='', tags=('notification_icon', 'notification_dot'))


class NotificationUI(CommunityTasks):
    def _build_notification_bell(self, parent):
        import slg_gui as g
        button = _BellButton(parent, fg_color=g.CHIP, hover_color=g.CARD_HOVER,
            text_color=g.TEXT, text_color_disabled=g.MUTED, command=self._toggle_notifications)
        button._canvas.bind('<Escape>', lambda event: self._close_notifications(), add='+')
        self._notification_bell = button
        button.bind('<Configure>', lambda event: self._position_notifications(), add='+')
        self._refresh_notification_badge()
        return button

    def _toggle_notifications(self):
        win = getattr(self, '_notification_window', None)
        if win is not None and win.winfo_exists():
            self._close_notifications()
        else:
            self._open_notifications()

    def _close_notifications(self):
        win = getattr(self, '_notification_window', None)
        self._notification_window = None
        if win is not None and win.winfo_exists(): win.destroy()

    def _position_notifications(self):
        win = getattr(self, '_notification_window', None)
        bell = getattr(self, '_notification_bell', None)
        if win is None or not win.winfo_exists() or bell is None or not bell.winfo_exists(): return
        scale = ctk.ScalingTracker.get_window_scaling(win)
        left, top = bell.winfo_vrootx(), bell.winfo_vrooty()
        right, bottom = left + bell.winfo_vrootwidth(), top + bell.winfo_vrootheight()
        width = min(440, max(280, int((right-left-16)/scale)))
        height = min(620, max(240, int((bottom-top-24)/scale)))
        x = bell.winfo_rootx()+bell.winfo_width()-width*scale
        y = bell.winfo_rooty()+bell.winfo_height()+6*scale
        if y+height*scale > bottom-8: y = bell.winfo_rooty()-height*scale-6*scale
        x = max(left+8, min(x, right-width*scale-8))
        y = max(top+8, min(y, bottom-height*scale-8))
        win.geometry(f'{width}x{height}+{int(x)}+{int(y)}')

    def _notification_outside(self, event):
        for widget in (getattr(self, '_notification_window', None), getattr(self, '_notification_bell', None)):
            if widget is not None and widget.winfo_exists():
                x, y = widget.winfo_rootx(), widget.winfo_rooty()
                if x <= event.x_root < x+widget.winfo_width() and y <= event.y_root < y+widget.winfo_height(): return
        self._close_notifications()

    def _notification_projection(self):
        provider = getattr(self, '_notification_announcements', None)
        value = provider() if provider else {}
        return value if isinstance(value, dict) else {}

    def _refresh_notification_badge(self):
        button = getattr(self, '_notification_bell', None)
        if button is None or not button.winfo_exists(): return
        # Do not use notification unread counts, notice timestamps or unknown status.
        dot = reward_dot(self._notification_projection(), self._has_usable_cloud_session())
        button.set_dot(dot)

    def _refresh_notifications(self, page=1):
        page = max(1, int(page))
        # A pending page may be reused when the user returns to it. Its callback
        # reads the latest page intention rather than an obsolete call serial.
        self._notification_requested_page = page
        self._refresh_notification_badge()
        if not self._has_usable_cloud_session():
            self._notification_snapshot = None
            self._notification_identity = self._library_identity()
            self._render_notifications()
            return
        identity = self._library_identity()
        if getattr(self, '_notification_identity', None) != identity:
            self._notification_snapshot = None
            self._notification_identity = identity
        self._notification_loading = True
        self._notification_snapshot = None
        self._notification_error = None
        self._render_notifications()
        def done(value, error):
            if page != getattr(self, '_notification_requested_page', None): return
            self._notification_loading = False
            self._notification_snapshot = None if error else value
            self._notification_error = error
            self._render_notifications()
        self._community_request(('notifications', page), lambda: api.fetch_notifications(page), done)

    def _open_notifications(self):
        import slg_gui as g
        chip = getattr(self, '_account_chip', None)
        if chip is not None:
            chip.close()
        win = getattr(self, '_notification_window', None)
        if win is not None:
            try:
                if win.winfo_exists(): win.lift(); self._refresh_notifications(); return win
            except tk.TclError: pass
        win = self._community_track_window(_AccountPopup(self))
        win.withdraw(); win.overrideredirect(True); win.transient(self.winfo_toplevel())
        win.configure(fg_color=g.CARD)
        self._notification_window = win
        top = ctk.CTkFrame(win, fg_color='transparent'); top.pack(fill='x', padx=14, pady=(10,0))
        ResourceLabel(top, text='消息', font=g.ui_font(size=16, weight='bold'), text_color=g.TEXT).pack(side='left')
        ResourceButton(top, text='×', width=32, height=32, fg_color=g.CHIP, text_color=g.TEXT, command=self._close_notifications).pack(side='right')
        self._notification_body = ctk.CTkScrollableFrame(win, fg_color=g.CARD)
        self._notification_body.pack(fill='both', expand=True, padx=8, pady=8)
        self._notification_structure_signature = None
        self._notification_reply_section = None
        self._render_notifications()
        win.bind_all('<Button-1>', self._notification_outside, add='+')
        win.bind_all('<Escape>', lambda event: self._close_notifications(), add='+')
        win.update_idletasks(); self._position_notifications(); win.deiconify()
        refresh = getattr(self, '_notification_refresh_announcements', None)
        if refresh: refresh()
        self._refresh_notifications()
        return win

    def _render_notifications(self):
        import slg_gui as g
        win = getattr(self, '_notification_window', None)
        if win is None: return
        try:
            if not win.winfo_exists(): return
        except Exception: return
        body = self._notification_body
        projection = self._notification_projection()
        def heading(text):
            ResourceLabel(body, text=text, anchor='w', font=g.ui_font(size=15, weight='bold'), text_color=g.TEXT).pack(fill='x', padx=8, pady=(12, 6))
        def note(text):
            ResourceLabel(body, text=text, wraplength=370, justify='left', anchor='w', text_color=g.MUTED, font=g.ui_font(size=12)).pack(fill='x', padx=8, pady=4)
        signature = json.dumps(projection, ensure_ascii=False, sort_keys=True, default=str)
        section = getattr(self, '_notification_reply_section', None)
        if signature != getattr(self, '_notification_structure_signature', None) or section is None or not section.winfo_exists():
            for child in body.winfo_children(): child.destroy()
            heading('公告')
            announcements = projection.get('announcements', [])
            for item in announcements if isinstance(announcements, list) else []:
                if not isinstance(item, dict): continue
                callback = getattr(self, '_open_notification_announcement', None)
                ResourceButton(body, text=str(item.get('title') or '查看公告')[:80], height=38, fg_color=g.CHIP, text_color=g.TEXT,
                    command=lambda item=item, callback=callback: callback(item.get('id')) if callback else None).pack(fill='x', padx=8, pady=3)
            if not announcements: note('暂无公告；可打开公告详情查看历史内容。')
            if not announcements and hasattr(self, '_open_notification_announcement'):
                ResourceButton(body, text='查看公告', height=36, fg_color=g.CHIP, text_color=g.TEXT, command=lambda: self._open_notification_announcement(None)).pack(fill='x', padx=8)
            heading('活动')
            note(str(projection.get('activity_text') or '活动状态暂未同步'))
            if reward_dot(projection, self._has_usable_cloud_session()):
                ResourceButton(body, text='查看可领取活动积分', height=38, fg_color=g.ACCENT, hover_color=g.ACCENT_HOVER, text_color=g.ON_ACCENT, text_color_disabled=g.MUTED,
                    command=lambda: self._open_notification_announcement(projection.get('activity_id'))).pack(fill='x', padx=8, pady=4)
            heading('回复')
            section = self._notification_reply_section = ctk.CTkFrame(body, fg_color='transparent')
            section.pack(fill='x')
            self._notification_structure_signature = signature
        body = section
        for child in body.winfo_children(): child.destroy()
        if not self._has_usable_cloud_session(): note('登录后查看回复消息'); return
        if getattr(self, '_notification_loading', False): note('正在同步回复消息…')
        error = getattr(self, '_notification_error', None)
        snapshot = getattr(self, '_notification_snapshot', None)
        if error:
            note('回复消息读取失败；未显示旧内容。' + str(error))
            ResourceButton(body, text='重试', height=36, fg_color=g.CHIP, text_color=g.TEXT, command=self._refresh_notifications).pack(fill='x', padx=8)
            return
        if not isinstance(snapshot, dict): return
        note(f'未读 {int(snapshot.get("unread_count") or 0)} 条')
        notifications = snapshot.get('notifications', [])
        for item in notifications:
            if not isinstance(item, dict): continue
            text = ('未读 · ' if item.get('read') is not True else '') + str(item.get('nickname') or '用户')[:40] + ' 回复了你'
            ResourceButton(body, text=text, height=42, fg_color=g.CHIP, text_color=g.TEXT,
                command=lambda item=item: self._open_reply_context(item.get('reply_id'), item.get('id'))).pack(fill='x', padx=8, pady=3)
        if not notifications: note('暂无可查看的回复消息')
        def mark_all():
            def done(value, error):
                if error:
                    self._notification_error = '标记已读失败：' + error; self._render_notifications()
                else: self._refresh_notifications()
            self._community_request('notification-read-all', lambda: api.mark_notifications_read(all=True), done, win)
        ResourceButton(body, text='全部标为已读', height=36, fg_color=g.CHIP, text_color=g.TEXT,
            text_color_disabled=g.MUTED,
            command=mark_all, state='normal' if notifications else 'disabled').pack(fill='x', padx=8, pady=8)
        page = int(snapshot.get('page') or 1)
        total_pages = int(snapshot.get('total_pages') or 0)
        pager = ctk.CTkFrame(body, fg_color='transparent'); pager.pack(fill='x', padx=8)
        pager.grid_columnconfigure((0, 1), weight=1, uniform='notification_pages')
        for column, (title, target, enabled) in enumerate([('上一页', page - 1, page > 1), ('下一页', page + 1, page < total_pages)]):
            ResourceButton(pager, text=title, width=1, height=34, fg_color=g.CHIP,
                hover_color=g.CARD_HOVER, text_color=g.TEXT, text_color_disabled=g.MUTED,
                state='normal' if enabled else 'disabled', command=lambda p=target: self._refresh_notifications(p)).grid(
                    row=0, column=column, sticky='ew', padx=(0, 4) if column == 0 else (4, 0))
