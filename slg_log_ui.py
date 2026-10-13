"""On-demand local error-log viewer; file reads never run on the Tk thread."""
import os
import threading

import customtkinter as ctk
from slg_image_resources import ResourceButton, ResourceLabel
import slg_crash


class LogUI:
    def open_error_log(self):
        import slg_gui as gui

        def color(name):
            return (gui._LIGHT[name], gui._DARK[name])

        win = self._new_dialog("本地错误日志", "740x560")
        win.configure(fg_color=color("BG"))
        info = ResourceLabel(win, text="日志只保存在本机，不会自动上传。分享前请检查内容。",
                             text_color=color("MUTED"), font=gui.ui_font(size=12))
        info.pack(fill="x", padx=16, pady=(14, 6))
        path_label = ResourceLabel(win, text="", text_color=color("TEXT"),
                                   anchor="w", justify="left", font=gui.ui_font(size=12))
        path_label.pack(fill="x", padx=16, pady=4)
        status = ResourceLabel(win, text="", text_color=color("MUTED"),
                               anchor="w", font=gui.ui_font(size=12))
        status.pack(fill="x", padx=16, pady=4)
        text = ctk.CTkTextbox(win, fg_color=color("CARD"), text_color=color("TEXT"),
                             scrollbar_button_color=color("CHIP"),
                             scrollbar_button_hover_color=color("CARD_HOVER"),
                             font=gui.ui_font(size=12), wrap="word")
        text.pack(fill="both", expand=True, padx=16, pady=8)
        text.configure(state="disabled")
        selected = {"path": None, "pending": False}
        paths = slg_crash.available_logs()
        menu = ctk.CTkOptionMenu(win, values=paths or ["当前日志"],
                                fg_color=color("CHIP"), button_color=color("CHIP"),
                                button_hover_color=color("CARD_HOVER"),
                                text_color=color("TEXT"), font=gui.ui_font(size=12))
        menu.pack(fill="x", padx=16, pady=(0, 6))
        actions = ctk.CTkFrame(win, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 14))

        def complete(result, error):
            if not win.winfo_exists():
                return
            selected["pending"] = False
            refresh_btn.configure(state="normal")
            menu.configure(state="normal")
            if error:
                status.configure(text="读取失败：" + slg_crash.redact(str(error)))
                return
            selected["path"] = result["path"]
            path_label.configure(text="位置：" + str(result["path"]))
            content = result["text"] or "尚无错误记录。"
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", content)
            text.configure(state="disabled")
            text.see("end")
            status.configure(text=("读取失败：" + result["error"] if result.get("error") else
                                   "已读取最近记录（最多256 KiB）；点击刷新可重新读取。"))

        def refresh(path=None):
            if selected["pending"]:
                return
            selected["pending"] = True
            refresh_btn.configure(state="disabled")
            menu.configure(state="disabled")
            status.configure(text="读取中…")
            chosen = path if path is not None else (menu.get() if paths else None)
            def worker():
                try:
                    value, error = slg_crash.read_log_tail(chosen), None
                except Exception as exc:
                    value, error = None, str(exc)
                self.queue.put(("library_callback", (complete, value, error)))
            threading.Thread(target=worker, daemon=True, name="local-error-log-reader").start()

        def open_folder():
            path = selected["path"]
            if not path:
                return
            folder = os.path.dirname(path)
            try:
                if not os.path.isdir(folder):
                    status.configure(text="日志目录尚未创建。")
                    return
                os.startfile(folder)
            except (OSError, AttributeError) as exc:
                status.configure(text="无法打开目录：" + slg_crash.redact(str(exc)))

        common = dict(fg_color=color("CHIP"), text_color=color("TEXT"),
                      text_color_disabled=color("MUTED"), hover_color=color("CARD_HOVER"),
                      font=gui.ui_font(size=12))
        refresh_btn = ResourceButton(actions, text="刷新", command=refresh, **common)
        refresh_btn.pack(side="left", padx=(0, 8))
        ResourceButton(actions, text="打开所在目录", command=open_folder, **common).pack(side="left")
        menu.configure(command=refresh)
        def fit(event=None):
            if (not win.winfo_exists() or not info.winfo_exists()
                    or not path_label.winfo_exists() or not status.winfo_exists()):
                return
            width = max(100, int(win.winfo_width() / ctk.ScalingTracker.get_window_scaling(win)) - 40)
            info.configure(wraplength=width)
            path_label.configure(wraplength=width)
            status.configure(wraplength=width)
        win.bind("<Configure>", fit, add="+")
        self._error_log_dialog = dict(win=win, text=text, status=status, path=selected,
                                      refresh=refresh, menu=menu)
        refresh()
