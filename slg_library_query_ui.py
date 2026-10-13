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
        win = self._new_dialog("查询与确认游戏资料", "680x680")
        color = lambda name: (gui._LIGHT[name], gui._DARK[name])
        win.configure(fg_color=color("BG"))
        stop = threading.Event()
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
        ResourceLabel(win, text="只发送下面的查询词或编号，不发送游戏路径。确认后才会加入本地库。",
                      text_color=color("MUTED"), wraplength=620).pack(padx=18, pady=10)
        source = ctk.CTkOptionMenu(win, values=["DLsite", "VNDB"], text_color=color("TEXT"),
                                   fg_color=color("CHIP"), button_color=color("CHIP"))
        source.pack(fill="x", padx=18, pady=4)
        source.set("DLsite" if item.get("product_ids") or item.get("online_sources")==["dlsite"] else "VNDB")
        query = ctk.CTkEntry(win, text_color=color("TEXT"), fg_color=color("CARD"))
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
            ResourceLabel(win, text="也可选择识别到的名称；选择后再点击查询。",
                          text_color=color("MUTED")).pack(padx=18, pady=2)
            def choose_title(value):
                if not state["busy"]:
                    query.delete(0, "end")
                    query.insert(0, title_choices[value])
            ctk.CTkOptionMenu(win, values=list(title_choices)[:16], command=choose_title,
                             text_color=color("TEXT"), fg_color=color("CHIP"),
                             button_color=color("CHIP")).pack(fill="x", padx=18, pady=4)
        status = ResourceLabel(win, text="选择来源后点击查询；离线或无结果时仍可手动添加。",
                               text_color=color("MUTED"), wraplength=620)
        status.pack(fill="x", padx=18, pady=6)
        results = ResourceScrollableFrame(win, fg_color=color("CARD"), height=130)
        results.pack(fill="both", expand=True, padx=18, pady=4)
        preview = ResourceLabel(win, text="尚未选择资料", text_color=color("TEXT"), wraplength=620)
        preview.pack(fill="x", padx=18, pady=8)
        cover = ResourceLabel(win, text="", text_color=color("MUTED"))
        cover.pack(pady=4)
        use_cover = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(win, text="采用上面预览的封面（替换个人封面）", variable=use_cover,
                        text_color=color("TEXT")).pack(padx=18, pady=5)
        common = dict(fg_color=color("CHIP"), text_color=color("TEXT"),
                      hover_color=color("CARD_HOVER"), text_color_disabled=color("MUTED"))

        def work(fn, complete):
            if state["busy"] or not alive():
                return
            state["busy"] = True
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

        search_btn = ResourceButton(win, text="查询资料", command=search, **common)
        search_btn.pack(fill="x", padx=18, pady=4)
        accept = ResourceButton(win, text="确认采用所选资料", command=confirm, state="disabled", **common)
        accept.pack(fill="x", padx=18, pady=(4, 12))
        self._metadata_query_dialog = dict(win=win, state=state, search=search, query=query,
                                           source=source, pick=pick, confirm=confirm, status=status)
