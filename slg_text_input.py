"""Readable, non-modal text input using the application's theme and scaling."""
import customtkinter as ctk
from slg_image_resources import ResourceButton, ResourceLabel


def open_text_input(owner, title, prompt, initial, submit):
    import slg_gui as g
    win = owner._new_dialog(title, "480x265")
    colors = {k: (g._LIGHT[k], g._DARK[k]) for k in
              ("BG", "CARD", "TEXT", "MUTED", "CHIP", "ACCENT", "ACCENT_HOVER", "ON_ACCENT", "DANGER_TEXT")}
    win.configure(fg_color=colors["BG"])
    ResourceLabel(win, text=prompt, text_color=colors["TEXT"],
                  font=g.ui_font(size=16, weight="bold"), wraplength=430).pack(fill="x", padx=24, pady=(22, 10))
    entry = ctk.CTkEntry(win, height=44, font=g.ui_font(size=14),
                        fg_color=colors["CARD"], text_color=colors["TEXT"], border_color=colors["CHIP"])
    entry.insert(0, initial or "")
    entry.pack(fill="x", padx=24)
    error = ResourceLabel(win, text="", text_color=colors["DANGER_TEXT"], font=g.ui_font(size=12), wraplength=430)
    error.pack(fill="x", padx=24, pady=8)
    row = ctk.CTkFrame(win, fg_color="transparent")
    row.pack(fill="x", padx=24, pady=(4, 20))
    def accept(event=None):
        name = entry.get().strip()
        if not name:
            error.configure(text="请输入收藏夹名称")
            return "break"
        try:
            result = submit(name)
        except Exception as exc:
            error.configure(text=str(exc))
            return "break"
        if result is not False and win.winfo_exists():
            win.destroy()
        return "break"
    ResourceButton(row, text="取消", width=100, height=40, font=g.ui_font(size=14),
                   fg_color=colors["CHIP"], text_color=colors["TEXT"], command=win.destroy).pack(side="right")
    ResourceButton(row, text="确认", width=100, height=40, font=g.ui_font(size=14),
                   fg_color=colors["ACCENT"], hover_color=colors["ACCENT_HOVER"],
                   text_color=colors["ON_ACCENT"], command=accept).pack(side="right", padx=10)
    entry.bind("<Return>", accept)
    entry.bind("<KP_Enter>", accept)
    win._text_input = dict(entry=entry, error=error, accept=accept)
    win.after_idle(lambda: entry.focus_set() if win.winfo_exists() else None)
    return win
