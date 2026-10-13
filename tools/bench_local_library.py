"""Offline real-Tk library/cover benchmark; measures layout, not display scanout."""
import argparse
import contextlib
import http.client
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def summarize(values):
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "median_ms": None, "p95_ms": None, "max_ms": None}
    return {"samples": len(values), "median_ms": statistics.median(values),
            "p95_ms": ordered[max(0, math.ceil(len(values) * .95) - 1)],
            "max_ms": max(values)}


def benchmark(args):
    with tempfile.TemporaryDirectory(prefix="slg-library-bench-", ignore_cleanup_errors=True) as data, \
         contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {key: data for key in
            ("LOCALAPPDATA", "APPDATA", "HOME", "USERPROFILE")}))
        if args.source_root:
            sys.path.insert(0, str(args.source_root.resolve()))
        import slg_account
        import slg_db
        import slg_gui
        import slg_remote
        import slg_translate
        from PIL import Image

        if not hasattr(slg_gui.App, "set_library_view"):
            return {"status": "unsupported", "source_root": str(Path(slg_gui.__file__).parent),
                    "reason": "This baseline has no unified local-library API; no timings fabricated."}

        def offline(*args, **kwargs):
            raise RuntimeError("network forbidden in library benchmark")

        for obj, name, kwargs in (
            (urllib.request, "urlopen", {"side_effect": offline}),
            (urllib.request.OpenerDirector, "open", {"side_effect": offline}),
            (http.client.HTTPConnection, "request", {"side_effect": offline}),
            (slg_account, "request", {"side_effect": offline}),
            (slg_account, "session", {"return_value": None}),
            (slg_remote, "report", {"return_value": None}),
            (slg_remote, "fetch_config", {"return_value": None}),
            (slg_translate, "translate_title", {"side_effect": offline}),
            (slg_translate, "translate_overview", {"side_effect": offline}),
            (slg_gui.App, "_load_dlsite_pilot", {"return_value": None}),
            (slg_gui.App, "_require_personal_access", {"return_value": True}),
        ):
            stack.enter_context(patch.object(obj, name, **kwargs))
        try:
            import requests
        except ImportError:
            pass
        else:
            stack.enter_context(patch.object(requests.Session, "request", side_effect=offline))
        # Initialize Pillow plugins on Tk's future owning thread. Opening a
        # worker's first plugin otherwise can trigger unrelated Tk-font GC.
        Image.init()
        conn = slg_db.connect()
        folder = Path(data) / "installed-game"
        folder.mkdir()
        covers = Path(slg_db.covers_dir())
        covers.mkdir(parents=True, exist_ok=True)
        cover_files = []
        for i in range(40):
            name = "bench-cover-%02d.png" % i
            Image.new("RGB", (320, 480), (i * 6 % 256, 75, 140)).save(covers / name)
            cover_files.append(name)
        for i in range(args.count):
            if i % 10 == 0:
                gid = slg_db.add_user_game(conn, "Bench Custom %05d" % i,
                                          categories=["galgame"], folder_path=str(folder))
            else:
                catalogue = "western_slg" if i % 2 else "dlsite"
                slug = "bench-western-%05d" % i if catalogue == "western_slg" else "dlsite-rj%08d" % i
                gid, _ = slg_db.upsert_game(conn, slug, "https://example.invalid/" + slug,
                    "Bench Public %05d" % i, tags=["common"], complete=0)
                slg_db.set_game_catalogue(conn, gid, catalogue)
                if catalogue == "dlsite":
                    slg_db.set_dlsite_metadata(conn, gid, {"work_type": "game", "languages": ["ja"]})
                slg_db.set_local_folder(conn, gid, str(folder))
            conn.execute("UPDATE games SET cover_file=? WHERE id=?",
                         (cover_files[i % len(cover_files)], gid))
        conn.commit()
        conn.close()
        app = slg_gui.App(notify=False)
        slg_db.set_pref(app.conn, 'library.onboarding_seen', '1')
        app.geometry("1280x820")
        errors = []
        app.report_callback_exception = lambda *values: errors.append(str(values))
        try:
            def wait(predicate, timeout=15):
                deadline, failures = time.perf_counter() + timeout, []
                def poll():
                    try:
                        if predicate():
                            app.quit()
                            return
                        if time.perf_counter() > deadline:
                            raise RuntimeError("benchmark UI wait timed out")
                    except BaseException as exc:
                        failures.append(exc)
                        app.quit()
                        return
                    app.after(2, poll)
                app.after(0, poll)
                app.tk.mainloop()
                if failures:
                    raise failures[0]

            def rendered():
                return (not getattr(app, "_card_render_pending", False)
                        and not (app.origin == "local" and getattr(app, "_library_query_pending", False)))

            wait(rendered)
            wait(lambda: not app._cover_waiters)
            beat_alive, last_beat, gaps = [True], [time.perf_counter()], []
            def beat():
                now = time.perf_counter()
                gaps.append((now - last_beat[0]) * 1000)
                last_beat[0] = now
                if beat_alive[0]:
                    app.after(30, beat)
            app.after(30, beat)

            def measure(action):
                started = time.perf_counter()
                first = []
                action()
                app.after_idle(lambda: first.append((time.perf_counter() - started) * 1000))
                wait(lambda: bool(first) and rendered())
                layout = (time.perf_counter() - started) * 1000
                wait(lambda: not app._cover_waiters)
                return first[0], layout, (time.perf_counter() - started) * 1000

            # Bidirectional changes every round, including warmup; invoking
            # an already-active view is never counted as a successful switch.
            cold_library = measure(app.set_library_view)
            measure(lambda: app.set_view(None))
            for _ in range(3):
                measure(app.set_library_view)
                measure(lambda: app.set_view(None))
            samples = {direction: [[], [], []] for direction in ("public_to_library", "library_to_public")}
            stages = {}
            for _ in range(args.rounds):
                for name, action in (("public_to_library", app.set_library_view),
                                     ("library_to_public", lambda: app.set_view(None))):
                    values = measure(action)
                    for bucket, value in zip(samples[name], values):
                        bucket.append(value)
                    if name == 'public_to_library':
                        for key, value in getattr(app, '_library_last_query_timings', {}).items():
                            stages.setdefault(key, []).append(value)
            app.set_library_view()
            wait(rendered)
            warm_pages = [[], [], []]
            page_changer = getattr(app, '_goto_page', None)
            if page_changer is not None and getattr(app, '_library_total', len(app.rows)) > 8:
                for i in range(args.rounds):
                    values = measure(lambda i=i: page_changer(2 if i % 2 == 0 else 1))
                    for bucket, value in zip(warm_pages, values):
                        bucket.append(value)
            cover_first, cover_prepared = [], []
            game = app.rows[0]
            for i in range(args.rounds):
                started, first = time.perf_counter(), []
                app.open_cover_editor(game)
                editor = app._cover_editor
                app.after_idle(lambda: first.append((time.perf_counter() - started) * 1000))
                wait(lambda: bool(first))
                cover_first.append(first[0])
                chosen = str(covers / cover_files[i % len(cover_files)])
                decode_started = time.perf_counter()
                import slg_library_ui
                with patch.object(slg_library_ui.filedialog, "askopenfilename", return_value=chosen):
                    editor["choose"]()
                wait(lambda: editor["state"]["ready"])
                cover_prepared.append((time.perf_counter() - decode_started) * 1000)
                # Close through normal lifecycle after preview; no database
                # writes or disk-game execution are part of this benchmark.
                editor["win"].destroy()
            interaction_gaps = list(gaps)
            steady_started = time.perf_counter()
            gaps.clear()
            last_beat[0] = steady_started
            wait(lambda: time.perf_counter() - steady_started >= 4)
            beat_alive[0] = False
            metrics = {name: {key: summarize(values) for key, values in zip(
                ("first_idle_frame", "layout_complete", "covers_ready"), buckets)}
                for name, buckets in samples.items()}
            metrics.update(cover_dialog_first_frame=summarize(cover_first),
                           cover_preview_ready=summarize(cover_prepared),
                           cold_public_to_library={key: value for key, value in zip(
                               ('first_idle_frame_ms', 'layout_complete_ms', 'covers_ready_ms'), cold_library)},
                           warm_library_paging={key: summarize(values) for key, values in zip(
                               ('first_idle_frame', 'layout_complete', 'covers_ready'), warm_pages)},
                           local_query_stages={key: summarize(values) for key, values in stages.items()})
            return {"status": "ok" if not errors else "tk_errors", "count": args.count,
                    "rounds": args.rounds, "custom_count": args.count // 10,
                    "cover_file_count": len(cover_files), "source_root": str(Path(slg_gui.__file__).parent),
                    "window": "1280x820", "dpi": app.winfo_fpixels("1i"),
                    "metrics": metrics, "interaction_tick_gap": summarize(interaction_gaps),
                    "steady_tick_gap": summarize(gaps), "steady_duration_seconds": 4,
                    "tk_errors": errors, "network": "blocked", "data": "temporary isolated profile",
                    "note": "Actual public/local navigation; warm covers after 3 warmups. First frame means first Tk idle, not display scanout. Cover preview decodes a real image each round. Shared synthetic installed path measures rendering, not import collisions."}
        finally:
            app.destroy()
            app.conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, choices=(1000, 10000), default=1000)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    if args.rounds < 2:
        parser.error("rounds must be at least 2")
    result = benchmark(args)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
