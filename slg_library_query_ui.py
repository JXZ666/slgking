"""User-triggered provider queries and explicit personal import confirmation."""
import os
import ntpath
import tempfile
import threading
import tkinter as tk
import customtkinter as ctk
import slg_db
import slg_library_metadata as metadata
import slg_local_library as library
from slg_image_resources import ResourceButton, ResourceLabel, ResourceScrollableFrame


class LibraryQueryUI:
    def open_metadata_query(self, item, on_select=None):
        import slg_gui as gui
        import slg_library_providers as providers
        identity = self._library_identity()
        win = self._new_dialog("查询与确认游戏资料", "1020x740")
        color = lambda name: (gui._LIGHT[name], gui._DARK[name])
        win.configure(fg_color=color("BG"))
        header = ctk.CTkFrame(win, fg_color="transparent")
        header.pack(fill="x", padx=4, pady=(4, 0))
        footer = ctk.CTkFrame(win, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=14, pady=10)
        panes = ctk.CTkFrame(win, fg_color="transparent")
        panes.pack(fill="both", expand=True, padx=18, pady=5)
        panes.grid_columnconfigure(0, weight=1)
        panes.grid_columnconfigure(1, weight=2)
        panes.grid_rowconfigure(0, weight=1)
        stop = threading.Event()
        win._slg_before_destroy = stop.set
        state = {"busy": False, "candidate": None, "cover_file": None, "applied": False}
        cover_directory = os.path.realpath(slg_db.covers_dir())
        owned_covers, retained_covers = {}, set()
        cover_lock = threading.Lock()

        def remove_owned(records):
            # Only files created by this dialog, and still unchanged, are ours.
            for path, signature in records:
                try:
                    stat = os.stat(path)
                    current = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    if current == signature:
                        os.unlink(path)
                except OSError:
                    pass

        def cleanup_covers():
            with cover_lock:
                records = [record for name, record in owned_covers.items() if name not in retained_covers]
                for name in list(owned_covers):
                    if name not in retained_covers:
                        del owned_covers[name]
            remove_owned(records)

        def track_cover(name, before):
            if not isinstance(name, str) or name in before or os.path.basename(name) != name:
                return
            path = os.path.realpath(os.path.join(cover_directory, name))
            if os.path.dirname(path) != cover_directory:
                return
            stat = os.stat(path)
            record = (path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
            with cover_lock:
                if not stop.is_set():
                    owned_covers[name] = record
                    return
            remove_owned([record])

        def alive():
            return win.winfo_exists() and identity == self._library_identity() and not stop.is_set()
        def destroyed(event):
            if event.widget == win:
                stop.set()
                cleanup_covers()
        win.bind("<Destroy>", destroyed, add="+")
        privacy_note = ResourceLabel(header, text="只发送下面的查询词或编号，不发送游戏路径。确认后才会加入本地库。",
                      text_color=color("MUTED"), wraplength=620, font=gui.ui_font(size=12))
        privacy_note.pack(padx=18, pady=10)
        source = ctk.CTkOptionMenu(header, values=["DLsite", "VNDB"], text_color=color("TEXT"),
                                   fg_color=color("CHIP"), button_color=color("CHIP"))
        source.pack(fill="x", padx=18, pady=4)
        source.set("DLsite" if item.get("product_ids") or item.get("online_sources")==["dlsite"] else "VNDB")
        query = ctk.CTkEntry(header, text_color=color("TEXT"), fg_color=color("CARD"),
                                height=40, font=gui.ui_font(size=14))
        query.pack(fill="x", padx=18, pady=4)
        hints = item.get("evidence", {}).get("titles", [])
        # Folder names are more reliable than helper executable ProductNames.
        trusted_hint = next((h["text"] for h in hints if isinstance(h, dict)
                             and not h.get("ambiguous") and isinstance(h.get("text"), str)), "")
        default_query = (item.get("product_ids") or [item.get("name", "").strip()
                         or trusted_hint])[0]
        query.insert(0, default_query)
        title_choices = {default_query: default_query}
        for hint in hints:
            if not isinstance(hint, dict) or not isinstance(hint.get("text"), str):
                continue
            label = hint["text"]
            if hint.get("ambiguous"):
                label += "（编码 %s，请核对）" % hint.get("encoding", "未知")
            title_choices.setdefault(label, hint["text"])
        if len(title_choices) > 1:
            ResourceLabel(header, text="也可选择识别到的名称；选择后再点击查询。",
                          text_color=color("MUTED")).pack(padx=18, pady=2)
            def choose_title(value):
                if not state["busy"]:
                    query.delete(0, "end")
                    query.insert(0, title_choices[value])
            ctk.CTkOptionMenu(header, values=list(title_choices)[:16], command=choose_title,
                             text_color=color("TEXT"), fg_color=color("CHIP"),
                             button_color=color("CHIP")).pack(fill="x", padx=18, pady=4)
        status = ResourceLabel(header, text="选择来源后点击查询；离线或无结果时仍可手动添加。",
                               text_color=color("MUTED"), wraplength=620, font=gui.ui_font(size=12))
        status.pack(fill="x", padx=18, pady=6)
        results = ResourceScrollableFrame(panes, fg_color=color("CARD"), height=150)
        results.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        detail_panel = ResourceScrollableFrame(panes, fg_color=color("CARD"))
        detail_panel.grid(row=0, column=1, sticky="nsew")
        preview = ResourceLabel(detail_panel, text="尚未选择资料", text_color=color("TEXT"),
                                font=gui.ui_font(size=13), wraplength=440, justify="left")
        preview.pack(fill="x", padx=14, pady=12)
        cover = ResourceLabel(detail_panel, text="", text_color=color("MUTED"))
        cover.pack(pady=8)
        use_cover = tk.BooleanVar(value=False)
        cover_check = ctk.CTkCheckBox(detail_panel, text="采用预览封面（替换个人封面）", variable=use_cover,
                        font=gui.ui_font(size=13), text_color=color("TEXT"))
        cover_check.pack(padx=14, pady=12)
        layout = {"wide": None}
        def resize(event=None):
            if stop.is_set() or not win.winfo_exists(): return
            scale = ctk.ScalingTracker.get_widget_scaling(panes)
            wide = panes.winfo_width() / scale >= 760
            if wide != layout["wide"]:
                layout["wide"] = wide
                detail_panel.grid_forget()
                results.grid_forget()
                panes.grid_columnconfigure(1, weight=2 if wide else 0)
                panes.grid_rowconfigure(1, weight=0 if wide else 2)
                results.grid(row=0, column=0, sticky="nsew", padx=(0, 10) if wide else 0,
                             pady=0 if wide else (0, 8))
                detail_panel.grid(row=0 if wide else 1, column=1 if wide else 0, sticky="nsew")
            width = max(180, int(detail_panel.winfo_width() / scale) - 32)
            preview.configure(wraplength=width)
            header_width = max(180, int(header.winfo_width() / scale) - 40)
            privacy_note.configure(wraplength=header_width)
            status.configure(wraplength=header_width)
        panes.bind("<Configure>", resize, add="+")
        detail_panel.bind("<Configure>", resize, add="+")
        common = dict(fg_color=color("CHIP"), text_color=color("TEXT"),
                      hover_color=color("CARD_HOVER"), text_color_disabled=color("MUTED"), height=40, font=gui.ui_font(size=13))

        def work(fn, complete):
            if state["busy"] or not alive():
                return
            state["busy"] = True
            status.configure(text="联网查询可能需要一些时间，请稍候；你可以随时取消。")
            search_btn.configure(state="disabled")
            accept.configure(state="disabled")
            def worker():
                try:
                    value, error = fn(), None
                except Exception as exc:
                    value, error = None, str(exc)
                self.queue.put(("library_callback", (finish, (value, error, complete), None)))
            threading.Thread(target=worker, daemon=True, name="personal-metadata-query").start()

        def finish(payload, ignored):
            value, error, complete = payload
            state["busy"] = False
            if not alive():
                cleanup_covers()
                if isinstance(value, tuple) and len(value) == 4 and value[1] is not None:
                    value[1].close()
                return
            search_btn.configure(state="normal")
            if error:
                status.configure(text="查询失败：" + error)
                return
            complete(value)

        def pick(candidate):
            if state["busy"]:
                return
            state["candidate"], state["cover_file"] = None, None
            cleanup_covers()
            use_cover.set(False)
            cover.configure(image=None, text="")
            status.configure(text="读取所选作品资料与封面…")
            def load():
                facts = metadata.validate(providers.details(candidate["source"], candidate["external_id"], should_stop=stop.is_set))
                if (facts["source"] != candidate["source"] or
                        facts["external_id"].casefold() != candidate["external_id"].casefold()):
                    raise ValueError("所选候选与详情返回的来源或编号不一致，请重新查询。")
                image, name, warning = None, None, ""
                if facts.get("cover_url") and not stop.is_set():
                    try:
                        raw = providers.download_cover(facts["cover_url"], facts["source"], should_stop=stop.is_set)
                        fd, path = tempfile.mkstemp(suffix=".img")
                        try:
                            with os.fdopen(fd, "wb") as output:
                                output.write(raw)
                            image = library.prepare_cover(path)
                            image.thumbnail((180, 180))
                            try:
                                before = set(os.listdir(cover_directory))
                            except FileNotFoundError:
                                before = set()
                            name = library.save_cover(image)
                            track_cover(name, before)
                        finally:
                            os.unlink(path)
                    except Exception as exc:
                        warning = "封面未取得，可继续使用资料：" + str(exc)
                return facts, image, name, warning
            def show(value):
                facts, image, name, warning = value
                state["candidate"], state["cover_file"] = facts, name
                preview.configure(text="\n".join([facts["title"], facts["source"] + " · " + facts["external_id"],
                    "开发者：" + (facts["developer"] or "未知"), "日期：" + (facts["release_date"] or "未知"), facts["source_url"]]))
                if image:
                    cover.configure(image=ctk.CTkImage(light_image=image, dark_image=image, size=image.size), text="")
                status.configure(text=warning or "请核对作品与封面，再确认。")
                accept.configure(state="normal")
            work(load, show)

        def search():
            term = query.get().strip()
            if not term or state["busy"]:
                return
            if (len(term)>512 or "\\" in term or term.startswith(("/", "file:"))
                    or (not term.startswith(("https://", "http://")) and ntpath.splitdrive(term)[0])):
                status.configure(text="请输入作品名称、编号或官方链接，不要输入本机路径。")
                return
            status.configure(text="正在查询…")
            provider = "dlsite" if source.get() == "DLsite" else "vndb"
            state["candidate"] = None
            state["cover_file"] = None
            cover.configure(image=None, text="")
            cleanup_covers()
            def show(candidates):
                for child in results.winfo_children():
                    child.destroy()
                for candidate in candidates[:10]:
                    ResourceButton(results, text=candidate["title"] + " · " + candidate["external_id"],
                        command=lambda c=candidate: pick(c), **common).pack(fill="x", padx=5, pady=3)
                status.configure(text="请选择候选查看详情，不会自动采用第一条。" if candidates else "未找到资料，可修改查询词或手动添加。")
            work(lambda: providers.search(provider, term, should_stop=stop.is_set), show)

        def confirm():
            if state["busy"] or not state["candidate"] or not alive():
                return
            candidate = dict(state["candidate"])
            aliases = list(candidate.get("aliases", []))
            folder_name = item.get("name", "").strip()
            if folder_name and len(folder_name) <= 512 and folder_name not in aliases:
                aliases.append(folder_name)
            candidate["aliases"] = aliases[:64]
            candidate["personal_cover_file"] = state["cover_file"] if use_cover.get() else None
            with cover_lock:
                candidate["personal_cover_owned"] = bool(candidate["personal_cover_file"] and
                    candidate["personal_cover_file"] in owned_covers)
            if on_select is not None:
                if on_select(candidate) is False:
                    status.configure(text="原扫描窗口已关闭，请重新扫描或查询。")
                    return
            else:
                if not self._require_personal_access("导入游戏资料"):
                    return
                try:
                    with slg_db.session() as conn:
                        gid = metadata.confirm(conn, candidate)
                        if candidate["personal_cover_file"]:
                            library.set_cover_override(conn, gid, candidate["personal_cover_file"])
                        conn.commit()
                except Exception as exc:
                    status.configure(text="导入失败，未保存：" + str(exc))
                    return
            if candidate["personal_cover_file"]:
                with cover_lock:
                    retained_covers.add(candidate["personal_cover_file"])
            state["applied"] = True
            win.destroy()
            if on_select is None:
                self._library_personal_changed()
                self._library_focus_gid = gid
                if self.origin == "local":
                    self.refresh()
                else:
                    self.set_library_view()

        search_btn = ResourceButton(footer, text="查询资料", command=search, **common)
        search_btn.pack(side="left", fill="x", expand=True, padx=4)
        accept = ResourceButton(footer, text="确认采用所选资料", command=confirm, state="disabled", **common)
        accept.pack(side="left", fill="x", expand=True, padx=4)
        ResourceButton(footer, text="取消", command=win.destroy, width=80, **common).pack(side="right", padx=4)
        self._metadata_query_dialog = dict(win=win, state=state, search=search, query=query,
                                           source=source, pick=pick, confirm=confirm, status=status, panes=panes,
                                           footer=footer, preview=preview, cover=cover, stop=stop,
                                           detail_panel=detail_panel, results=results, layout=layout, resize=resize)
