"""User-triggered provider queries and explicit personal import confirmation."""
import os
import ntpath
import tempfile
import threading
import tkinter as tk
import customtkinter as ctk
import slg_db
import slg_library_metadata as metadata
from slg_source_registry import SOURCES, valid_id
from slg_candidate_widgets import (
    CandidateRow, LookupBar, LookupPanes, OfficialLinkPanel, WrappedLabel,
    enable_button_keys,
)
import slg_local_library as library
from slg_image_resources import ResourceButton, ResourceLabel


class LibraryQueryUI:
    def open_metadata_query(self, item, on_select=None, initial_candidate=None):
        import slg_gui as gui
        import slg_library_providers as providers
        identity = self._library_identity()
        win = self._new_dialog("查询与确认游戏资料", "1020x740")
        color = lambda name: (gui._LIGHT[name], gui._DARK[name])
        win.configure(fg_color=color("BG"))
        header = ctk.CTkFrame(win, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(10, 0))
        footer = ctk.CTkFrame(win, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=10)
        panes = LookupPanes(win, color=color, font=gui.ui_font)
        panes.pack(fill="both", expand=True, padx=18, pady=5)
        stop = threading.Event()
        state = {"busy": False, "candidate": None, "cover_file": None,
                 "cover_image": None, "applied": False, "generation": 0}
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
            return not stop.is_set() and win.winfo_exists() and identity == self._library_identity()

        common = dict(fg_color=color("CHIP"), text_color=color("TEXT"),
                      hover_color=color("CARD_HOVER"), text_color_disabled=color("MUTED"),
                      height=40, font=gui.ui_font(size=13))
        bar = LookupBar(header, [entry.label for entry in SOURCES.values() if entry.search],
                        lambda: search(), color=color, font=gui.ui_font)
        bar.pack(fill="x")
        source, query = bar.source, bar.query
        preferred = next((key for key in item.get("online_sources", [])
                          if key in SOURCES and SOURCES[key].search), None)
        source.set(SOURCES[preferred].label if preferred else "DLsite" if item.get("product_ids") else "VNDB")
        hints = item.get("evidence", {}).get("titles", [])
        # Folder names are more reliable than helper executable ProductNames.
        trusted_hint = next((h["text"] for h in hints if isinstance(h, dict)
                             and not h.get("ambiguous") and isinstance(h.get("text"), str)), "")
        default_query = (item.get("product_ids") or [item.get("name", "").strip()
                         or trusted_hint])[0]
        bar.term.set(default_query)
        title_choices = {default_query: default_query}
        for hint in hints:
            if not isinstance(hint, dict) or not isinstance(hint.get("text"), str):
                continue
            label = hint["text"]
            if hint.get("ambiguous"):
                label += "（编码 %s，请核对）" % hint.get("encoding", "未知")
            title_choices.setdefault(label, hint["text"])
        secondary = ctk.CTkFrame(header, fg_color="transparent")
        secondary.pack(fill="x", pady=(6, 0))
        ai_button = ResourceButton(secondary, text="AI辅助识别…",
                                   command=lambda: assisted(), **{**common, "height": 32})
        ai_button.pack(side="right")
        enable_button_keys(ai_button)
        suggestions = None
        if len(title_choices) > 1:
            ResourceLabel(secondary, text="识别到的名称", text_color=color("MUTED"),
                          font=gui.ui_font(size=12)).pack(side="left", padx=(0, 8))

            def choose_title(value):
                if alive() and not state["busy"]:
                    bar.term.set(title_choices[value])

            choices = [label for label in title_choices if title_choices[label]][:16]
            suggestions = ctk.CTkOptionMenu(
                secondary, values=choices, command=choose_title, width=1,
                dynamic_resizing=False, text_color=color("TEXT"), fg_color=color("CHIP"),
                button_color=color("CHIP"), dropdown_fg_color=color("CARD"),
                dropdown_text_color=color("TEXT"))
            suggestions.pack(side="left", fill="x", expand=True, padx=(0, 8))
        return_note = ("采用后回到扫描结果，确认导入时保存。"
                       if on_select is not None else "确认后添加到本地库。")
        WrappedLabel(header, viewport=header, text="查询仅发送作品名称、编号或链接。" + return_note,
                     text_color=color("MUTED"), font=gui.ui_font(size=12)).pack(
                         fill="x", pady=(7, 2))
        status = WrappedLabel(header, viewport=header, text="选择来源并输入查询词。",
                              text_color=color("TEXT"), font=gui.ui_font(size=12))
        status.pack(fill="x", pady=(2, 4))
        results, detail_panel = panes.results, panes.details
        result_note = WrappedLabel(results, viewport=results._parent_canvas, padding=24,
                                   text="查询结果会显示在这里。", text_color=color("MUTED"),
                                   font=gui.ui_font(size=12))
        result_note.pack(fill="x", padx=12, pady=8)
        choose_note = "查看候选详情后，可在这里核对作品和来源。"
        preview = WrappedLabel(detail_panel, viewport=detail_panel._parent_canvas, padding=28,
                               text=choose_note, text_color=color("TEXT"), font=gui.ui_font(size=13))
        preview.pack(fill="x", padx=14, pady=12)
        links = OfficialLinkPanel(detail_panel, color=color, font=gui.ui_font, alive=alive)
        cover = ResourceLabel(detail_panel, text="", text_color=color("MUTED"))
        use_cover = tk.BooleanVar(master=win, value=False)
        cover_check = ctk.CTkCheckBox(detail_panel, text="采用此图作为个人封面", variable=use_cover,
                        font=gui.ui_font(size=13), text_color=color("TEXT"))

        def rows():
            return [child for child in results.winfo_children() if isinstance(child, CandidateRow)]

        def controls():
            busy = state["busy"]
            interactive = "disabled" if busy else "normal"
            bar.set_busy(busy)
            ai_button.configure(state=interactive)
            if suggestions is not None:
                suggestions.configure(state=interactive)
            for row in rows():
                row.choose_button.configure(state=interactive)
            accept.configure(state="normal" if not busy and state["candidate"] else "disabled")

        def clear_detail(text=choose_note):
            state["candidate"], state["cover_file"] = None, None
            links.clear()
            links.pack_forget()
            use_cover.set(False)
            cover_check.pack_forget()
            cover.configure(image=None)
            cover.pack_forget()
            image, state["cover_image"] = state["cover_image"], None
            if image is not None:
                image.close()
            cleanup_covers()
            preview.configure(text=text)
            detail_panel._parent_canvas.yview_moveto(0)
            accept.configure(state="disabled")

        def show_rows(candidates=(), note="查询结果会显示在这里。"):
            for row in rows():
                row.destroy()
            result_note.configure(text=note)
            for candidate in candidates[:10]:
                CandidateRow(results, candidate, pick, color=color, font=gui.ui_font,
                             alive=alive, action_text="查看详情").pack(fill="x", padx=8, pady=3)
            results._parent_canvas.yview_moveto(0)

        def work(task, show, failed, message):
            state["busy"] = True
            state["generation"] += 1
            token = state["generation"]
            status.configure(text=message)
            controls()

            def cancelled():
                return stop.is_set() or token != state["generation"]

            def finish(value, error):
                state["busy"] = False
                if not alive() or cancelled():
                    cleanup_covers()
                    if isinstance(value, tuple) and len(value) == 4 and value[1] is not None:
                        value[1].close()
                elif error is not None:
                    failed(error)
                else:
                    show(value)
                if alive():
                    controls()

            def worker():
                try:
                    value, error = task(cancelled), None
                except Exception as exc:
                    value, error = None, str(exc)[:180]
                self.queue.put(("library_callback", (finish, value, error)))

            threading.Thread(target=worker, daemon=True, name="personal-metadata-query").start()

        def pick(candidate):
            if state["busy"] or not alive():
                return False
            if not getattr(self, "_network_enabled", True):
                status.configure(text="当前处于离线模式。可取消并手动处理，或联网后重试。")
                return False
            key, external = candidate.get("source"), candidate.get("external_id")
            provider = SOURCES.get(key)
            if not provider or not provider.details or not valid_id(key, external):
                status.configure(text="所选资料的来源或编号无效，请重新查询。")
                return False
            gid = item.get("game_id")
            if gid and not metadata.query_allowed(self.conn, gid, key):
                status.configure(text="此游戏已关闭该来源查询，可在管理→资料来源中开启。")
                return False
            # AI can hand a candidate directly to the same explicit review path.
            def selected(row):
                return (row.candidate.get("source"), row.candidate.get("external_id")) == (key, external)

            if source.get() != provider.label or not any(selected(row) for row in rows()):
                source.set(provider.label)
                show_rows([candidate], note="核对所选候选的详情。")
            for row in rows():
                row.set_selected(selected(row))
            clear_detail("正在读取所选作品的详情…")

            def load(cancelled):
                if key == "dlsite":
                    facts = providers.structured_details(external, should_stop=cancelled)
                else:
                    facts = providers.details(key, external, should_stop=cancelled)
                facts = metadata.validate(facts)
                if facts["source"] != key or facts["external_id"].casefold() != external.casefold():
                    raise ValueError("所选候选与详情返回的来源或编号不一致，请重新查询。")
                image, name, warning = None, None, ""
                if facts.get("cover_url") and not cancelled():
                    try:
                        raw = providers.download_cover(facts["cover_url"], key, should_stop=cancelled)
                        if cancelled():
                            return facts, None, None, ""
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
                        name = None
                        warning = "封面未取得，可继续使用资料：" + str(exc)[:120]
                return facts, image, name, warning

            def show(value):
                facts, image, name, warning = value
                state["candidate"], state["cover_file"] = facts, name
                state["cover_image"] = image
                preview.configure(text="\n".join([facts["title"], provider.label + " · " + facts["external_id"],
                    "开发者：" + (facts["developer"] or "未知"), "日期：" + (facts["release_date"] or "未知")]))
                if links.set_candidate(facts):
                    links.pack(fill="x", padx=14, pady=(0, 10))
                if image is not None:
                    cover.configure(image=ctk.CTkImage(light_image=image, dark_image=image, size=image.size), text="")
                    cover.pack(padx=14, pady=(0, 6))
                    if name:
                        cover_check.pack(anchor="w", padx=14, pady=(0, 12))
                status.configure(text=warning or "核对标题和来源编号后，确认采用这份资料。")

            def failed(error):
                preview.configure(text="详情读取失败。可再次查看此候选，或选择其他作品。")
                status.configure(text="读取详情失败：" + error)

            work(load, show, failed, "正在读取所选作品的资料，可能需要一些时间。可随时取消。")
            return True

        def search():
            if state["busy"] or not alive():
                return
            if not getattr(self, "_network_enabled", True):
                status.configure(text="当前处于离线模式。可取消并手动处理，或联网后重试。")
                return
            term = query.get().strip()
            if not term:
                status.configure(text="请输入作品名称、编号或官方链接。")
                return
            lower = term.lower()
            if (len(term) > 512 or "\\" in term or lower.startswith(("/", "file:"))
                    or (not lower.startswith(("https://", "http://")) and ntpath.splitdrive(term)[0])):
                status.configure(text="请输入作品名称、编号或官方链接，不要输入本机路径。")
                return
            provider = next(key for key, entry in SOURCES.items() if entry.label == source.get())
            gid = item.get("game_id")
            if gid and not metadata.query_allowed(self.conn, gid, provider):
                status.configure(text="此游戏已关闭该来源查询，可在管理→资料来源中开启。")
                return
            clear_detail()
            show_rows(note="正在查询候选…")

            def show(candidates):
                if candidates:
                    show_rows(candidates, note=f"找到 {min(len(candidates), 10)} 项候选。")
                    status.configure(text="查看候选详情，核对作品身份后再采用。")
                else:
                    show_rows(note="未找到候选。")
                    status.configure(text="未找到资料。可修改查询词、切换来源，或取消并手动处理。")

            def failed(error):
                show_rows(note="查询未完成。")
                status.configure(text="查询失败：" + error + "。可重试、修改查询词或切换来源。")

            work(lambda cancelled: providers.lookup_variants(provider, [term], should_stop=cancelled),
                 show, failed, "正在查找候选，可能需要一些时间。可随时取消。")

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
                generation = state["generation"]
                if not self._require_personal_access("导入游戏资料"):
                    return
                if not alive() or generation != state["generation"]:
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

        def reset_lookup(message):
            if not alive():
                return
            state["generation"] += 1
            clear_detail()
            show_rows()
            status.configure(text=message)

        def assisted():
            if state['busy'] or not alive():
                return
            from slg_ai_identification_ui import open_ai_identification
            generation = state["generation"]
            evidence = dict(item, title=query.get().strip())

            def chosen(_index, candidate):
                nonlocal generation
                if alive() and generation == state["generation"] and pick(candidate):
                    generation = state["generation"]
                    win.lift()

            open_ai_identification(self, [evidence], on_provider=chosen, owner=win)

        accept_text = "采用资料，返回扫描结果" if on_select is not None else "添加到本地库"
        accept = ResourceButton(footer, text=accept_text, command=confirm, state="disabled",
                                fg_color=color("ACCENT"), hover_color=color("ACCENT_HOVER"),
                                text_color=color("ON_ACCENT"), text_color_disabled=color("MUTED"),
                                height=40, font=gui.ui_font(size=13))
        accept.pack(side="right")
        cancel = ResourceButton(footer, text="取消", command=win.destroy, width=80, **common)
        cancel.pack(side="right", padx=(0, 8))
        for button in (cancel, accept):
            enable_button_keys(button)
        source.configure(command=lambda _label: reset_lookup("来源已切换，请重新查询并核对作品。"))
        trace = bar.term.trace_add("write", lambda *_: reset_lookup("查询词已修改，请重新查询。"))

        def destroyed(event):
            if event.widget == win:
                stop.set()
                bar.term.trace_remove("write", trace)
                cleanup_covers()
                if state["cover_image"] is not None:
                    state["cover_image"].close()
                    state["cover_image"] = None

        win._slg_before_destroy = stop.set
        win.bind("<Destroy>", destroyed, add="+")
        self._metadata_query_dialog = dict(win=win, state=state, search=search, query=query,
                                           source=source, pick=pick, confirm=confirm, status=status, panes=panes,
                                           footer=footer, preview=preview, cover=cover, stop=stop,
                                           detail_panel=detail_panel, results=results, layout=panes.layout,
                                           resize=panes.fit_width, links=links, cover_check=cover_check,
                                           accept=accept)
        if initial_candidate is not None:
            from slg_motion import MotionScheduler
            MotionScheduler(win).call_later(1,lambda:pick(initial_candidate) if alive() else None)
