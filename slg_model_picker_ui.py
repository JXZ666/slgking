"""A manual model field with an on-demand, searchable provider list."""

from collections import OrderedDict
import time
import tkinter as tk

import customtkinter as ctk

from slg_candidate_widgets import WrappedLabel, enable_button_keys
from slg_image_resources import ResourceButton, ResourceScrollbar, ResourceToplevel
from slg_model_catalog import ModelListError, ModelListJob, model_list_request


_CACHE_TTL = 60
_CACHE_LIMIT = 8
_ERRORS = {
    "endpoint": "请填写 HTTPS 接口地址。本机服务可使用回环 HTTP 地址。",
    "key": "请先填写此接口的 API Key。",
    "busy": "上次请求仍在结束，请稍后刷新。",
    "timeout": "读取模型列表超时。",
    "network": "无法连接模型列表接口。",
    "empty": "服务商没有返回可选模型。",
    "invalid": "服务商返回的模型列表格式异常。",
    "oversize": "模型列表超出响应大小上限。",
}
_HTTP_ERRORS = {
    401: "服务商未接受当前 Key（HTTP 401）。",
    403: "当前 Key 无权读取模型列表（HTTP 403）。",
    404: "此接口未提供模型列表（HTTP 404）。",
    429: "模型列表请求过于频繁（HTTP 429），请稍后刷新。",
}
_MODALITIES = {"text": "文本", "image": "图像", "audio": "音频", "video": "视频", "file": "文件"}


def _error_text(error):
    if error.code == "http" and isinstance(error.status, int):
        if 300 <= error.status < 400:
            message = f"模型列表接口返回重定向（HTTP {error.status}），请核对地址。"
        else:
            message = _HTTP_ERRORS.get(error.status, f"无法读取模型列表（HTTP {error.status}）。")
    else:
        message = _ERRORS.get(error.code, _ERRORS["network"])
    return message + "可手动填写模型 ID。"


def _button(parent, text, command, primary=False):
    import slg_gui as g

    button = ResourceButton(parent, text=text, command=command, width=102, height=36,
                            corner_radius=8, font=g.ui_font(size=13),
                            fg_color=g.ACCENT if primary else g.CHIP,
                            hover_color=g.ACCENT_HOVER if primary else g.CARD_HOVER,
                            text_color=g.ON_ACCENT if primary else g.TEXT,
                            text_color_disabled=g.MUTED)
    enable_button_keys(button)
    button.bind("<FocusIn>", lambda _e: button.configure(border_width=2, border_color=g.TEXT))
    button.bind("<FocusOut>", lambda _e: button.configure(border_width=0))
    return button


class ModelPicker(ctk.CTkFrame):
    """Mount a model field. The caller owns draft credentials and identity lifetime.

    All callbacks run on Tk. get_credentials returns (base_url, eligible_key).
    Return a missing key if the draft is not authorized for its current endpoint.
    get_revision must change on every relevant draft edit, including A→B→A.
    """

    def __init__(self, parent, *, app, model_var, get_credentials, identity,
                 is_current, get_revision):
        import slg_gui as g

        super().__init__(parent, fg_color="transparent", width=1)
        self.app = app
        self.model_var = model_var
        self.get_credentials = get_credentials
        self.identity = identity
        self.is_current = is_current
        self.get_revision = get_revision
        self._cache_identity = tuple(sorted(identity.items())) if isinstance(identity, dict) else identity
        if not hasattr(app, "_slg_model_cache"):
            app._slg_model_cache = OrderedDict()
        self._cache = app._slg_model_cache
        self._popup = None
        self.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(self, textvariable=model_var, width=1, height=36,
                                  corner_radius=8, font=g.ui_font(size=13))
        self.entry.grid(row=0, column=0, sticky="ew")
        self.button = _button(self, "选择模型", self.open)
        self.button.grid(row=0, column=1, padx=(8, 0))
        tk.Misc.bind(self, "<Unmap>", self._unmapped, add="+")

    def _unmapped(self, event):
        if event.widget is self:
            self.invalidate()

    def open(self):
        if not self.is_current():
            self.invalidate()
            return
        if self._popup is not None and self._popup.revision == self.get_revision():
            self._popup.lift()
            self._popup.search.focus_set()
            return
        self.invalidate()
        self._popup = _ModelPopup(self)
        self._popup.load()

    def invalidate(self):
        if self._popup is not None:
            self._popup.destroy()

    def destroy(self):
        self.invalidate()
        super().destroy()


class _ModelPopup(ResourceToplevel):
    def __init__(self, picker):
        import slg_gui as g

        super().__init__(picker.app, fg_color=g.BG)
        self._closed = False
        self.withdraw()
        self.picker = picker
        self.revision = picker.get_revision()
        self._slg_identity_context = picker.identity
        self._slg_before_destroy = self._dispose
        self._job = None
        self._models = None
        self._visible = ()
        self.title("选择模型")
        self.transient(picker.winfo_toplevel())
        self.minsize(355, 315)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=14)
        self.query = tk.StringVar(master=self)
        self.search = ctk.CTkEntry(body, textvariable=self.query, height=36,
                                   font=g.ui_font(size=13))
        self.search.pack(fill="x")
        self.status = WrappedLabel(body, text="搜索模型 ID", text_color=g.MUTED,
                                   font=g.ui_font(size=12))
        self.status.pack(fill="x", pady=(4, 6))
        results = ctk.CTkFrame(body, fg_color=g.CARD, corner_radius=0)
        results.pack(fill="both", expand=True)
        results.grid_columnconfigure(0, weight=1)
        results.grid_rowconfigure(0, weight=1)
        self.results = tk.Listbox(results, height=4, width=1, exportselection=False,
                                  font=g.ui_tkfont(12, dpi_scale=body._get_widget_scaling()),
                                  background=g.CARD, foreground=g.TEXT, selectbackground=g.ACCENT,
                                  selectforeground=g.ON_ACCENT, relief="flat", highlightthickness=1,
                                  highlightcolor=g.ACCENT, highlightbackground=g.CHIP)
        self.results.grid(row=0, column=0, sticky="nsew")
        for orient, axis, row, col, sticky in (("vertical", "y", 0, 1, "ns"),
                                               ("horizontal", "x", 1, 0, "ew")):
            scrollbar = ResourceScrollbar(results, orientation=orient,
                                          command=getattr(self.results, axis + "view"))
            scrollbar.grid(row=row, column=col, sticky=sticky)
            self.results.configure(**{axis + "scrollcommand": scrollbar.set})
        self.capability = WrappedLabel(body, text="", text_color=g.MUTED, font=g.ui_font(size=12))
        self.capability.pack(fill="x", pady=(4, 8))
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill="x")
        self.refresh = _button(actions, "刷新列表", lambda: self.load(refresh=True))
        self.refresh.pack(side="left")
        self.choose = _button(actions, "使用所选模型", self.select, primary=True)
        self.choose.pack(side="right")
        _button(actions, "手动填写", lambda: self.dismiss(manual=True)).pack(side="right", padx=6)
        self._query_trace = self.query.trace_add("write", self._render)
        self.search.bind("<Down>", self._focus_results)
        self.results.bind("<<ListboxSelect>>", self._selection_changed)
        self.results.bind("<Return>", self.select)
        self.results.bind("<KP_Enter>", self.select)
        self.protocol("WM_DELETE_WINDOW", self.dismiss)
        self.bind("<Escape>", self.dismiss)
        if hasattr(picker.app, "_place"):
            picker.app._place(self, 540, 390, picker.winfo_toplevel())
        else:
            self.geometry("540x390")
        self.deiconify()
        self.lift()
        self.after_idle(self.search.focus_set)

    def _current(self):
        return (self.revision == self.picker.get_revision() and self.picker.is_current())

    def load(self, refresh=False):
        if self._job is not None:
            return
        if not self._current():
            self.destroy()
            return
        self._models = None
        self._render()
        try:
            request, fingerprint = model_list_request(*self.picker.get_credentials())
            key = self.picker._cache_identity, request.full_url, fingerprint
            cache = self.picker._cache
            now = time.monotonic()
            for expired in tuple(cache_key for cache_key, (until, _) in cache.items() if until <= now):
                del cache[expired]
            if key in cache:
                entry = cache.pop(key)
                if not refresh:
                    _, self._models = entry
                    cache[key] = entry
                    self._render()
                    return
            self._job = ModelListJob(request)
        except ModelListError as error:
            self.status.configure(text=_error_text(error))
            return
        self.status.configure(text="正在读取模型列表…")
        self.refresh.configure(state="disabled")
        self.after(40, self._poll, key)

    def _poll(self, key):
        if self.revision != self.picker.get_revision():
            self.destroy()
            return
        try:
            models = self._job.poll()
        except ModelListError as error:
            models = None
            message = _error_text(error)
        else:
            if models is None:
                self.after(40, self._poll, key)
                return
            message = ""
        if not self._current():
            self.destroy()
            return
        self._job = None
        self.refresh.configure(state="normal")
        if message:
            self.status.configure(text=message)
            return
        cache = self.picker._cache
        cache[key] = (time.monotonic() + _CACHE_TTL, models)
        cache.move_to_end(key)
        if len(cache) > _CACHE_LIMIT:
            cache.popitem(last=False)
        self._models = models
        self._render()

    def _render(self, *_):
        query = self.query.get().strip().casefold()
        self.results.delete(0, "end")
        self._visible = ()
        if self._models is not None:
            options = self._models.options
            self._visible = tuple(model for model in options if query in model.id.casefold())
            if self._visible:
                self.results.insert("end", *(model.id for model in self._visible))
                message = f"搜索模型 ID · {len(self._visible)} / {len(options)}"
            else:
                message = "没有匹配的模型。可修改搜索或手动填写模型 ID。"
            if self._models.truncated:
                message += f" · 仅显示服务商返回的前 {len(options)} 个模型"
            self.status.configure(text=message)
        self._selection_changed()

    def _focus_results(self, _event):
        if self._visible:
            self.results.focus_set()
            if not self.results.curselection():
                self.results.selection_set(0)
                self.results.activate(0)
                self._selection_changed()
        return "break"

    def _selection_changed(self, _event=None):
        selection = self.results.curselection()
        self.choose.configure(state="normal" if selection else "disabled")
        if not selection:
            text = "选择一项后可查看服务商声明的输入能力。"
        else:
            modalities = self._visible[selection[0]].input_modalities
            text = ("服务商声明的输入能力 · " + "、".join(_MODALITIES[kind] for kind in modalities)
                    if modalities else "服务商未声明输入能力。")
        self.capability.configure(text=text)

    def select(self, _event=None):
        selection = self.results.curselection()
        if not self._current():
            self.destroy()
        elif selection:
            model = self._visible[selection[0]].id
            self.dismiss(manual=True)
            self.picker.model_var.set(model)
        return "break"

    def dismiss(self, _event=None, *, manual=False):
        self.destroy()
        if self.picker.winfo_exists():
            if manual:
                self.picker.entry.focus_set()
            else:
                self.picker.button._text_label.focus_set()
        return "break"

    def _dispose(self):
        if self._job is not None:
            self._job.cancel()
        self.query.trace_remove("write", self._query_trace)
        if self.picker._popup is self:
            self.picker._popup = None

    def destroy(self):
        if not self._closed:
            self._closed = True
            super().destroy()
