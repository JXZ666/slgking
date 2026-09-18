"""Timing harness for the card list. Run before and after a rendering change:

    python tools/bench_render.py

Builds a throwaway database of 1000 games, opens the real window against it, and
times the three interactions that go through refresh(): switching view, changing
sort, and dragging a filter chip. Nothing here writes to the user's own database -
LOCALAPPDATA is redirected before slg_db is imported.
"""

import itertools
import os
import sys
import tempfile
import time

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="slgbench-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_gui  # noqa: E402

COUNT = 1000
SORT_LABELS = ("推荐分", "站内评分", "最近更新", "名称")


def seed():
    conn = slg_db.connect()
    for i in range(COUNT):
        gid, _ = slg_db.upsert_game(
            conn, "bench-%d" % i, "u/%d" % i, "Bench Game %d v1.0" % i,
            tags=["netorare", "big-tits", "anal", "bdsm", "story"],
            complete=i % 2)
        # A third of the catalogue carries a status, so the filtered views have
        # something to show. Without this the view switches measure an empty
        # list and every number comes back at ~18ms.
        if i % 3 == 0:
            slg_db.set_state(conn, gid, status="want")
        elif i % 3 == 1:
            slg_db.set_state(conn, gid, status="playing")
    # upsert_game and set_tags do not commit - the scrape pipeline owns that -
    # so a seed that just closes the connection rolls the whole thing back.
    conn.commit()
    return conn


def timeit(app, fn, rounds=3):
    best = float("inf")
    for _ in range(rounds):
        start = time.perf_counter()
        fn()
        app.update()
        best = min(best, time.perf_counter() - start)
    return best * 1000


def main():
    conn = seed()
    print("seeded %d games in %s" % (COUNT, slg_db.db_path()))
    conn.close()

    app = slg_gui.App(notify=False)
    app.geometry("1280x820")
    app.update()
    print("rows from find_games: %d, cards on screen: %d"
          % (len(app.rows), len(app.list.winfo_children())))

    def report(title, fn, rounds=3):
        print("  %-16s: %7.1f ms" % (title, timeit(app, fn, rounds)))

    # The cold build is the one cost the pool cannot remove: with no widgets to
    # reuse, a page of cards still has to be constructed. It is what the first
    # paint pays, and what rebuilding after a theme switch pays. This reproduces
    # what _teardown_ui leaves behind - clearing the pool without also destroying
    # the widgets would leak a screenful of orphans into every round.
    def cold():
        for child in app.list.winfo_children():
            child.destroy()
        app._card_pool, app._pool_gid = [], []
        app._cards, app._card_meta = {}, {}
        app._card_title, app._widget_gid = {}, {}
        app._more_btn = app._empty_label = None
        app.refresh()

    print("\n--- cold build, %d cards from nothing ---" % slg_gui.PAGE)
    report("cold render", cold, rounds=2)
    print("  Tk children     : %d" % len(app.list.winfo_children()))

    for shown in (slg_gui.PAGE, 480):
        # set_view and toggle_tag both reset `shown` back to PAGE, so `shown` is
        # set after the view, not before. Sorting keeps `shown`, which is why it
        # is the operation measured at both sizes.
        app.set_view(None)
        app.shown = shown
        app.refresh()
        app.update()
        print("\n--- %d cards on screen ---" % shown)
        print("  Tk children     : %d" % len(app.list.winfo_children()))

        labels = itertools.cycle(SORT_LABELS)
        report("sort change", lambda: app._on_sort(next(labels)))
        report("view switch", lambda: app.set_view("want"))

    # 显示更多 appends a page, so it pays a cold build for that page - the pool
    # has nothing to reuse for widgets that do not exist yet. This is the last
    # remaining lag the pool cannot remove.
    print("\n--- 显示更多, one press at a time ---")
    app.set_view(None)
    app.shown = slg_gui.PAGE
    app.refresh()
    app.update()
    for page in range(1, 5):
        report("press %d" % page, app._more, rounds=1)
    print("  cards on screen : %d" % len(app._card_pool))

    app.destroy()


if __name__ == "__main__":
    main()
