"""Offline real-Tk add-dialog benchmark against an explicit source directory."""
import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch


def stats(values):
    values = sorted(values)
    return dict(samples=len(values), median_ms=statistics.median(values),
                p95_ms=values[math.ceil(len(values)*.95)-1], max_ms=max(values))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--tags', type=int, default=1000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    with tempfile.TemporaryDirectory(prefix='slg-add-bench-', ignore_cleanup_errors=True) as temp, contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {'LOCALAPPDATA':temp, 'APPDATA':temp}))
        import slg_gui, slg_db, slg_account, slg_remote
        stack.enter_context(patch.object(slg_account, 'session', return_value=None))
        stack.enter_context(patch.object(slg_remote, 'report', return_value=None))
        stack.enter_context(patch.object(slg_gui.App, '_require_personal_access', return_value=True))
        app = slg_gui.App(notify=False)
        for i in range(args.tags):
            app.conn.execute('INSERT OR IGNORE INTO tags(name) VALUES (?)', ('tag-%05d'%i,))
        app.conn.commit()
        app.update()
        errors=[]
        app.report_callback_exception=lambda *exc: errors.append(str(exc))
        calls, first, ready = [], [], []
        def descendants(w):
            for c in w.winfo_children():
                yield c
                yield from descendants(c)
        try:
            for _ in range(args.samples):
                start=time.perf_counter()
                before=set(app.winfo_children())
                app.open_add_game()
                calls.append((time.perf_counter()-start)*1000)
                dialog=next(w for w in app.winfo_children() if w not in before and isinstance(w,slg_gui.ctk.CTkToplevel))
                app.update_idletasks()
                first.append((time.perf_counter()-start)*1000)
                deadline=time.perf_counter()+30
                def finished():
                    for widget in descendants(dialog):
                        if widget.__class__.__name__=='TagSelector':
                            return not widget.rendering
                        if isinstance(widget,slg_gui.FlowFrame) and len(widget._items)>=args.tags:
                            return True
                    return False
                def poll():
                    if finished() or time.perf_counter()>deadline:
                        app.quit()
                    else:
                        app.after(10,poll)
                app.after(0,poll)
                app.mainloop()
                if not finished():raise RuntimeError('Tag render timed out')
                ready.append((time.perf_counter()-start)*1000)
                dialog.destroy()
                app.update_idletasks()
            result={'source_root':str(args.source_root),'tags':args.tags,'open_call':stats(calls),
                    'mapped_form':stats(first),'tag_page_ready':stats(ready),'callback_errors':errors,
                    'measurement':'Tk layout and event completion, not physical display scanout; baseline renders all tags, candidate one page.'}
            raw=json.dumps(result,ensure_ascii=False,indent=2)
            if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(raw,encoding='utf-8')
            print(raw)
            if errors:raise SystemExit(1)
        finally:
            app.destroy()


if __name__=='__main__':main()
