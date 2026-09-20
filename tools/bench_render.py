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
SORT_LABELS = ("按xp推荐", "站内评分", "最近更新", "名称")


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
    # len(app._card_pool), not list.winfo_children(): the empty-state label is
    # built once as a child of the list and stays there unpacked, so counting
    # children is always one more than the number of cards.
    print("rows from find_games: %d, cards on screen: %d"
          % (len(app.rows), len(app._card_pool)))

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
        app._pager = app._pager_parts = app._empty_label = None
        app.page = 1
        app.refresh()

    pages = -(-len(app.rows) // slg_gui.PAGE_SIZE)
    print("\n--- cold build, %d cards from nothing ---" % slg_gui.PAGE_SIZE)
    report("cold render", cold, rounds=2)
    print("  cards built     : %d" % len(app._card_pool))

    # PAGE_SIZE is a fit, not a preference, so it is measured here: a card is
    # cover-driven, the cover is scaled by the display's factor, and the page
    # only looks right if the whole page lands above the fold. Scrolling is what
    # pagination replaced, so slack going negative is a regression even though
    # nothing raises.
    viewport = app.list._parent_canvas.winfo_height()
    card_h = app._card_pool[0]["frame"].winfo_height() + 4  # + the card's pady
    print("  scaling         : %.2f, card %d px, viewport %d px"
          % (app.list._get_widget_scaling(), card_h, viewport))
    print("  page slack      : %d px (%d of %d cards fit)"
          % (viewport - card_h * slg_gui.PAGE_SIZE, viewport // card_h,
             slg_gui.PAGE_SIZE))
    print("  pager visible   : %s" % bool(app._pager.winfo_ismapped()))


    # One page is the whole list now, so the per-interaction numbers are what
    # the user feels. Pagination also turns the old "how many cards are on
    # screen" knob into a fixed cost: refresh() always slices PAGE_SIZE rows.
    app.set_view(None)
    app.page = 1
    app.refresh()
    app.update()
    print("\n--- %d games over %d pages, %d per page ---"
          % (len(app.rows), pages, slg_gui.PAGE_SIZE))
    print("  cards on screen : %d" % len(app._card_pool))

    labels = itertools.cycle(SORT_LABELS)
    report("sort change", lambda: app._on_sort(next(labels)))
    report("view switch", lambda: app.set_view("want"))

    # Turning the page is the cost the old scrolling list paid on every wheel
    # tick. The pool replaces all six widgets rather than building new ones, so
    # this should land near a sort change rather than near a cold build.
    app.set_view(None)
    app.page = 1
    app.refresh()
    app.update()
    print("\n--- page turning ---")
    report("page next", lambda: app._goto_page(app.page + 1))
    report("page prev", lambda: app._goto_page(app.page - 1))
    # The worst case a typed page number can ask for: straight to page 100, so
    # find_games has to walk 594 rows to reach the six it returns.
    report("jump to 100", lambda: app._goto_page(100))
    print("  cards on screen : %d" % len(app._card_pool))
    print("  page after a jump: %d of %d" % (app.page, pages))

    app.destroy()


if __name__ == "__main__":
    main()
