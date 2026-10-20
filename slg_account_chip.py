"""Account summary presentation. Providers are local snapshots, never I/O calls."""
from dataclasses import dataclass
import math
import tkinter as tk
import customtkinter as ctk
from slg_image_resources import ResourceButton, ResourceLabel, ResourceToplevel


@dataclass(frozen=True)
class AccountSummary:
    identity: tuple
    nickname: str = "访客"
    balance: int | None = None
    status: str = "登录后同步积分与装扮"
    title: str = "未同步"
    avatar_frame: str = "未装备"
    comment_frame: str = "未装备"
    avatar_image: object = None
    logged_in: bool = False

    @property
    def initial(self):
        return (self.nickname[:1] or "游").upper()

    @property
    def balance_text(self):
        return "积分暂未同步" if self.balance is None else f"{self.balance:,} 积分"


def project_account(*, account_id=None, generation=0, admin=False, snapshot=None,
                    balance=None, balance_account_id=None, title_name=None,
                    avatar_frame_name=None, comment_frame_name=None,
                    developer_avatar=None, stale=False):
    """Only project same-account facts; callers resolve item names from local data.

    developer_avatar is an already decoded local image explicitly supplied for
    the developer identity. No credentials, paths or transport calls belong here.
    """
    identity = (bool(admin), account_id, generation)
    if not account_id:
        return AccountSummary(identity)
    cache = snapshot if isinstance(snapshot, dict) and snapshot.get("account_id") == account_id else {}
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    amount = balance if balance_account_id == account_id and number(balance) else cache.get("balance")
    amount = int(amount) if number(amount) else None
    # Auxiliary resolved names are usable only alongside a validated identity snapshot.
    return AccountSummary(identity, str(cache.get("nickname") or "用户")[:80], amount,
                          "最近同步 · 尚未重新确认" if stale else "最近同步" if cache or amount is not None else "资料暂未同步",
                          str(title_name or "默认头衔") if cache else "未同步",
                          str(avatar_frame_name or "未装备") if cache else "未同步",
                          str(comment_frame_name or "未装备") if cache else "未同步",
                          developer_avatar if admin and account_id == "developer" else None, True)


class _AccountPopup(ResourceToplevel):
    """Own CTk's constructor timers/global bindings as well as our content."""
    def __init__(self, *args, **kwargs):
        self._owned_after = set()
        self._owned_global = []
        super().__init__(*args, **kwargs)

    def after(self, ms, func=None, *args):
        if func is None: return super().after(ms)
        holder = []
        def run():
            if holder: self._owned_after.discard(holder[0])
            return func(*args)
        job = super().after(ms, run)
        holder.append(job)
        self._owned_after.add(job)
        return job

    def after_cancel(self, job):
        self._owned_after.discard(job)
        return super().after_cancel(job)

    def bind_all(self, sequence=None, func=None, add=None):
        fid = super().bind_all(sequence, func, add)
        if func is not None and fid: self._owned_global.append((sequence, fid))
        return fid

    def destroy(self):
        for job in tuple(self._owned_after):
            try: self.after_cancel(job)
            except tk.TclError: pass
        root = self._root()
        for seq, fid in self._owned_global:
            try: root._unbind(('bind', 'all', seq), fid)
            except tk.TclError: pass
        self._owned_global.clear()
        super().destroy()


class AccountChip(ctk.CTkFrame):
    """44-unit account control with an owned, disposable 300-unit popup.

    provider() -> AccountSummary must only read in-memory data. on_profile takes
    no arguments; owner performs its usual identity authorization. refresh() is
    called by existing account result handlers, with no polling or new reads.
    """
    def __init__(self, master, *, provider, on_profile, palette, on_show=None, screen_bounds=None):
        self.colors = dict(palette)
        super().__init__(master, fg_color="transparent", width=44, height=44)
        self.provider, self.on_profile, self.on_show = provider, on_profile, on_show
        self.screen_bounds = screen_bounds
        self.popup = None
        self.pinned = False
        self._disposed = False
        self._jobs = {}
        self._identity = None
        self._bindings = []
        self._hover_bindings = {}
        self.button = ResourceButton(self, text="游", width=44, height=44,
            corner_radius=22, border_width=1, border_color=self.colors['accent'],
            fg_color=self.colors['card'], hover_color=self.colors['hover'],
            text_color=self.colors['text'], command=self.toggle)
        self.pack_propagate(False)
        self.button.pack(fill="both", expand=True)
        self.button._canvas.configure(takefocus=1)
        self._sync_hover_bindings()
        canvas = self.button._canvas
        canvas.bind('<Return>', self._keyboard_open, add='+')
        canvas.bind('<space>', self._keyboard_open, add='+')
        canvas.bind('<Escape>', lambda _: self.close(), add='+')
        root = self._root()
        for sequence, callback in (('<Button-1>', self._outside), ('<Escape>', self._escape)):
            fid = root.bind_all(sequence, callback, add='+')
            self._bindings.append((sequence, fid))
        self.refresh()
        self.bind('<Destroy>', self._destroy_event, add='+')

    def _cancel(self, name):
        job = self._jobs.pop(name, None)
        if job:
            try: self.after_cancel(job)
            except tk.TclError: pass

    def _sync_hover_bindings(self):
        # CTk creates/replaces the native image label during configure(image=).
        # Bind the actual hit surfaces, not CTkButton.bind's current-child proxy.
        surfaces = {w for w in (self, self.button, self.button._canvas,
                    self.button._text_label, self.button._image_label) if w is not None}
        for widget, bindings in list(self._hover_bindings.items()):
            if widget not in surfaces:
                for sequence, fid in bindings:
                    try: tk.Misc.unbind(widget, sequence, fid)
                    except tk.TclError: pass
                self._hover_bindings.pop(widget, None)
        for widget in surfaces:
            if widget not in self._hover_bindings:
                self._hover_bindings[widget] = [
                    (sequence, tk.Misc.bind(widget, sequence, callback, add='+'))
                    for sequence, callback in (('<Enter>', self._enter), ('<Leave>', self._leave))]

    def _later(self, name, delay, callback):
        self._cancel(name)
        def run():
            self._jobs.pop(name, None)
            if not self._disposed: callback()
        self._jobs[name] = self.after(delay, run)

    def _enter(self, _=None):
        self._cancel('close')
        if self.popup is None: self._later('open', 180, self.open)

    def _leave(self, _=None):
        self._cancel('open')
        if not self.pinned: self._later('close', 200, self._close_if_outside)

    def _inside(self, widget):
        while widget is not None:
            if widget is self or widget is self.popup: return True
            widget = getattr(widget, 'master', None)
        return False

    def _close_if_outside(self):
        try:
            widget = self.winfo_containing(*self.winfo_pointerxy())
        except tk.TclError: widget = None
        if not self._inside(widget): self.close()

    def _outside(self, event):
        if self.popup is not None and not self._inside(event.widget): self.close()

    def _escape(self, _=None):
        if self.popup is not None: self.close(return_focus=True)

    def _keyboard_open(self, _=None):
        self.open(pinned=True)
        if self.popup is not None: self._profile_button._canvas.focus_set()
        return 'break'

    def toggle(self):
        if self.popup is not None and self.pinned: self.close()
        else: self.open(pinned=True)

    def refresh(self):
        if self._disposed: return
        state = self.provider()
        if not isinstance(state, AccountSummary): raise TypeError('provider must return AccountSummary')
        if self._identity is not None and state.identity != self._identity: self.close()
        self._identity = state.identity
        self.state = state
        self.button.configure(text='' if state.avatar_image else state.initial, image=state.avatar_image)
        self._sync_hover_bindings()
        # CTk reserves corner-radius padding in its internal grid. A true
        # 44-unit circle would otherwise leave no width for the nickname.
        for name in ('_text_label', '_image_label'):
            surface = getattr(self.button, name, None)
            if surface is not None:
                surface.grid_remove()
                surface.place_forget()
        surface = self.button._image_label if state.avatar_image else self.button._text_label
        if surface is not None:
            surface.place(relx=.5, rely=.5, anchor='center')
        if self.popup is not None: self._render_summary()

    def open(self, pinned=False):
        if self._disposed: return
        self._cancel('open'); self._cancel('close')
        self.refresh()
        self.pinned = self.pinned or pinned
        if self.popup is not None: return
        self.popup = _AccountPopup(self)
        self.popup.withdraw()
        self.popup.overrideredirect(True)
        self.popup.configure(fg_color=self.colors['card'])
        self.popup.transient(self.winfo_toplevel())
        self.popup.bind('<Enter>', self._enter, add='+')
        self.popup.bind('<Leave>', self._leave, add='+')
        self.popup.bind('<Escape>', self._escape, add='+')
        self._labels = {}
        for key in ('nickname', 'balance', 'status', 'title', 'avatar_frame', 'comment_frame'):
            label = ResourceLabel(self.popup, text='', anchor='w', justify='left',
                text_color=self.colors['muted'] if key == 'status' else self.colors['text'],
                wraplength=264)
            label.pack(fill='x', padx=18, pady=(12 if key == 'nickname' else 3, 0))
            self._labels[key] = label
        self._profile_button = ResourceButton(self.popup, text='', height=34,
            fg_color=self.colors['accent'], hover_color=self.colors['accent_hover'],
            text_color=self.colors['on_accent'], command=self._activate_profile)
        self._profile_button.pack(fill='x', padx=18, pady=14)
        self._profile_button._canvas.configure(takefocus=1)
        self._profile_button._canvas.bind('<Return>', lambda _: self._activate_profile(), add='+')
        self._profile_button._canvas.bind('<space>', lambda _: self._activate_profile(), add='+')
        self._profile_button._canvas.bind('<Tab>', self._tab_back, add='+')
        self._render_summary()
        self.popup.update_idletasks()
        self._position()
        self.popup.deiconify()
        if self.on_show: self.on_show(self.popup)

    def _render_summary(self):
        s = self.state
        values = dict(nickname=s.nickname, balance=s.balance_text, status=s.status,
                      title='头衔 · '+s.title, avatar_frame='头像框 · '+s.avatar_frame,
                      comment_frame='名片框 · '+s.comment_frame)
        for key, value in values.items(): self._labels[key].configure(text=value)
        self._profile_button.configure(text='个人中心' if s.logged_in else '登录账号')

    def _position(self):
        scale = ctk.ScalingTracker.get_window_scaling(self.popup)
        width = 300
        height = max(200, self.popup.winfo_reqheight()/scale)
        left, top = self.winfo_vrootx(), self.winfo_vrooty()
        right, bottom = left+self.winfo_vrootwidth(), top+self.winfo_vrootheight()
        if self.screen_bounds:
            left, top, right, bottom = self.screen_bounds(self.popup)
        x = self.winfo_rootx()+self.winfo_width()-width*scale
        y = self.winfo_rooty()+self.winfo_height()+6*scale
        if y+height*scale > bottom-8: y = self.winfo_rooty()-height*scale-6*scale
        x = max(left+8, min(x, right-width*scale-8))
        y = max(top+8, min(y, bottom-height*scale-8))
        self.popup._slg_anchor_position = (int(x), int(y))
        self.popup.geometry(f'{width}x{int(height)}+{int(x)}+{int(y)}')

    def _tab_back(self, _):
        self.close(return_focus=True)
        return 'break'

    def _activate_profile(self):
        identity = self.state.identity
        self.refresh()
        if identity != self.state.identity: return
        self.close()
        self.on_profile()

    def close(self, return_focus=False):
        self._cancel('open'); self._cancel('close')
        popup, self.popup = self.popup, None
        self.pinned = False
        if popup is not None:
            try: popup.destroy()
            except tk.TclError: pass
        if return_focus and not self._disposed:
            try: self.button._canvas.focus_set()
            except tk.TclError: pass

    def _destroy_event(self, event):
        if event.widget is self: self.dispose()

    def dispose(self):
        if self._disposed: return
        self.close()
        self._disposed = True
        root = self._root()
        for seq, fid in self._bindings:
            try: root._unbind(('bind', 'all', seq), fid)
            except tk.TclError: pass
        self._bindings.clear()
        for widget, bindings in list(self._hover_bindings.items()):
            for sequence, fid in bindings:
                try: tk.Misc.unbind(widget, sequence, fid)
                except tk.TclError: pass
        self._hover_bindings.clear()

    def destroy(self):
        self.dispose()
        super().destroy()
