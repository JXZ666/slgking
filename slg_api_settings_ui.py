"""Translation and AI settings embedded in the settings pane."""

import os
import re
import threading
import tkinter as tk

import customtkinter as ctk

import slg_db
import slg_engines
import slg_translate
from slg_candidate_widgets import WrappedLabel
from slg_image_resources import ResourceButton, ResourceLabel
from slg_model_catalog import ModelListError, models_endpoint
from slg_model_picker_ui import ModelPicker


_AI_LABEL = "AI 翻译"
_FREE_LABEL = "免费机翻"


def _credential_scope(provider, base):
    try:
        endpoint = models_endpoint(base)
    except ModelListError:
        endpoint = base.strip()
    return provider, endpoint


def build_api_settings(app, parent):
    """Mount the API form in an existing settings scroll frame."""
    import slg_gui as g

    config = slg_engines.resolve_config(app.conn)
    identity = app._capture_identity_context()
    scope = _credential_scope(config.provider, config.base_url)
    default_scope = _credential_scope(slg_engines.DEFAULT_PROVIDER, slg_engines.DEFAULT_BASE_URL)
    key = config.api_key
    if os.environ.get(slg_engines.ENV_KEY) and scope != default_scope:
        # The legacy DeepSeek environment override cannot bind itself to an edited endpoint.
        key = slg_db.get_pref(app.conn, slg_engines.PREF_KEY)
    providers = {preset[1]: preset for preset in slg_engines.PROVIDERS}
    frame = ctk.CTkFrame(parent, fg_color=g.CARD, corner_radius=10, width=1)
    frame.pack(fill="x", padx=12, pady=(12, 10))
    frame.grid_columnconfigure((0, 1), weight=1, uniform="api-actions")

    def label(master, row, message, color, size=13, *, columns=1, pady=(10, 4)):
        widget = WrappedLabel(master, viewport=parent._parent_canvas, padding=48,
                              text=message, text_color=color, font=g.ui_font(size=size))
        widget.grid(row=row, column=0, columnspan=columns, sticky="ew", padx=12, pady=pady)
        return widget

    ResourceLabel(frame, text="API 配置", text_color=g.TEXT, anchor="w",
                  font=g.ui_font(size=16, weight="bold")).grid(
        row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 0))
    intro = label(frame, 1, "", g.MUTED, 12, columns=2)
    label(frame, 2, "引擎", g.TEXT, columns=2)
    engine_seg = g.PaletteSegmentedButton(
        frame, values=[_AI_LABEL, _FREE_LABEL], height=36, font=g.ui_font(size=13))
    engine_seg.grid(row=3, column=0, columnspan=2, sticky="ew", padx=12)
    engine_seg.set(_FREE_LABEL if config.is_free else _AI_LABEL)

    ai = ctk.CTkFrame(frame, fg_color="transparent", width=1)
    ai.grid(row=4, column=0, columnspan=2, sticky="ew")
    ai.grid_columnconfigure(0, weight=1)
    label(ai, 0, "服务商", g.TEXT)
    provider_var = tk.StringVar(master=ai, value=slg_engines.PROVIDER_BY_ID[config.provider][1])
    provider_menu = ctk.CTkOptionMenu(
        ai, values=list(providers), variable=provider_var, width=1, height=36, corner_radius=8,
        fg_color=g.CHIP, text_color=g.TEXT, button_color=g.CHIP,
        button_hover_color=g.CARD_HOVER, font=g.ui_font(size=13), dynamic_resizing=False)
    provider_menu.grid(row=1, column=0, sticky="ew", padx=12)

    fields = {}
    for row, name, title, value in (
        (2, "base_url", "接口地址（/chat/completions 的上一级）", config.base_url),
        (4, "key", "API Key", key),
    ):
        label(ai, row, title, g.TEXT)
        variable = fields[name] = tk.StringVar(master=ai, value=value or "")
        entry = ctk.CTkEntry(ai, textvariable=variable, width=1, height=36,
                             corner_radius=8, font=g.ui_font(size=13),
                             show="•" if name == "key" else "")
        entry.grid(row=row + 1, column=0, sticky="ew", padx=12)
    key_hint = label(ai, 6, "服务商或接口地址已更改。请重新填写此接口的 API Key。",
                     g.MUTED, 12, pady=(4, 0))
    key_hint.grid_remove()
    label(ai, 7, "模型", g.TEXT)
    fields["model"] = tk.StringVar(master=ai, value=config.model or "")

    def values():
        return dict(
            engine=(slg_engines.ENGINE_FREE if engine_seg.get() == _FREE_LABEL
                    else slg_engines.ENGINE_OPENAI),
            provider=providers[provider_var.get()][0],
            **{name: variable.get().strip() for name, variable in fields.items()},
        )

    status = label(frame, 7, "", g.ACCENT, 12, columns=2, pady=(0, 10))
    revision = 0
    testing = False

    def current():
        return frame.winfo_exists() and app._capture_identity_context() == identity

    picker = ModelPicker(
        ai, app=app, model_var=fields["model"], identity=identity,
        get_credentials=lambda: (fields["base_url"].get(), fields["key"].get()),
        is_current=lambda: current() and engine_seg.get() == _AI_LABEL,
        get_revision=lambda: (revision, getattr(app, "_ai_config_generation", 0)),
    )
    picker.grid(row=8, column=0, sticky="ew", padx=12)

    def changed(*_):
        nonlocal revision
        revision += 1
        picker.invalidate()
        status.configure(text="配置已更改，上次测试结束后可重新测试。" if testing else "")

    def endpoint_changed(*_):
        nonlocal scope
        updated = _credential_scope(providers[provider_var.get()][0], fields["base_url"].get())
        if updated != scope:
            scope = updated
            if fields["key"].get():
                fields["key"].set("")
            key_hint.grid()
        changed()

    def key_changed(*_):
        if fields["key"].get().strip():
            key_hint.grid_remove()
        changed()

    def save():
        if current():
            picker.invalidate()
            app._save_translate_prefs(**values())

    def test_connection():
        nonlocal testing
        if testing or not current():
            return
        draft = values()
        trial = slg_engines.Config(draft["engine"], draft["provider"], draft["base_url"],
                                  draft["key"], draft["model"])
        if not trial.is_free and not trial.api_key:
            status.configure(text="请填写 API Key，或选择免费机翻。")
            return
        started = revision, getattr(app, "_ai_config_generation", 0)
        testing = True
        test_button.configure(state="disabled")
        status.configure(text="正在测试连接…")

        def finish(message, _error):
            nonlocal testing
            testing = False
            if not current():
                return
            test_button.configure(state="normal")
            if started == (revision, getattr(app, "_ai_config_generation", 0)) and draft == values():
                status.configure(text=message)
            elif status.cget("text").startswith("配置已更改"):
                status.configure(text="配置已更改，请重新测试。")

        def worker():
            try:
                slg_translate.probe(trial.api_key, trial.model, engine=trial.build())
                message = f"{trial.describe()} 连接正常。"
            except Exception as error:
                # Keep a useful HTTP status without showing provider response bodies.
                code = re.match(r"HTTP ([0-9]{3}):", str(error))
                hint = f"（HTTP {code[1]}）" if code else ""
                message = f"连接失败{hint}。请检查网络、接口地址、Key 和模型。"
            app.queue.put(("library_callback", (finish, message, None)))

        threading.Thread(target=worker, daemon=True).start()

    def translate_tags():
        if current():
            app.do_translate_tags(**values())

    button_options = dict(height=40, width=1, corner_radius=8, font=g.ui_font(size=13))
    ResourceButton(frame, text="保存", command=save, fg_color=g.ACCENT,
                   hover_color=g.ACCENT_HOVER, text_color=g.ON_ACCENT, **button_options).grid(
        row=5, column=0, sticky="ew", padx=(12, 4), pady=(14, 0))
    test_button = ResourceButton(frame, text="测试连接", command=test_connection,
                                 fg_color=g.CHIP, hover_color=g.CARD_HOVER, text_color=g.TEXT,
                                 text_color_disabled=g.MUTED, **button_options)
    test_button.grid(row=5, column=1, sticky="ew", padx=(4, 12), pady=(14, 0))
    tag_button = ResourceButton(frame, text="", command=translate_tags, fg_color=g.ACCENT,
                                hover_color=g.ACCENT_HOVER, text_color=g.ON_ACCENT,
                                text_color_disabled=g.ON_ACCENT, **button_options)
    tag_button.grid(row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 6))

    app._engine_seg = engine_seg
    app._settings_status = status
    app.tag_btn = tag_button

    def paint():
        if engine_seg.get() == _FREE_LABEL:
            intro.configure(text="免费机翻使用 Google 公开接口。AI 辅助识别和标签翻译需要选择 AI 翻译。")
            ai.grid_remove()
        else:
            intro.configure(text="选择兼容 OpenAI 的服务，填写接口地址、Key 和模型。AI 辅助识别共用此配置。")
            ai.grid()
        app._refresh_tag_button()

    def select_engine(_label):
        changed()
        paint()

    def select_provider(choice):
        _ident, _label, url, model = providers[choice]
        fields["base_url"].set(url)
        fields["model"].set(model)

    engine_seg.configure(command=select_engine)
    provider_menu.configure(command=select_provider)
    traces = [(field, field.trace_add("write", callback)) for field, callback in (
        (provider_var, endpoint_changed), (fields["base_url"], endpoint_changed),
        (fields["key"], key_changed), (fields["model"], changed),
    )]

    def destroy(event):
        if event.widget is not frame:
            return
        for field, trace in traces:
            field.trace_remove("write", trace)
        for name, widget in (("_engine_seg", engine_seg), ("_settings_status", status),
                             ("tag_btn", tag_button)):
            if getattr(app, name, None) is widget:
                setattr(app, name, None)

    tk.Misc.bind(frame, "<Destroy>", destroy, add="+")
    paint()
    return frame
