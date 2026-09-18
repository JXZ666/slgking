"""Drive the detail panel by hand: select, edit, switch, edit again.

Run with:

    python tools/smoke_detail.py

Uses a throwaway database so nothing here can touch the user's own library.
Prints one line per step and raises on the first thing that does not hold.
"""

import os
import sys
import tempfile

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="slgdetail-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_gui  # noqa: E402

WITH_OV = {"slug": "with-ov", "title": "Alpha Quest v1.20 [Studio X]",
           "overview": "You are trapped in a house.",
           "url": "https://dikgames.com/1", "developer": "Studio X",
           "version": "1.20"}
NO_OV = {"slug": "no-ov", "title": "Beta Tale", "overview": "", "url": "",
         "developer": "Studio Y", "version": None}


def seed():
    conn = slg_db.connect()
    ids = {}
    for row in (WITH_OV, NO_OV):
        gid, _ = slg_db.upsert_game(conn, row["slug"], "u/" + row["slug"],
                                    row["title"],
                                    tags=["netorare", "big-tits", "story"],
                                    developer=row["developer"],
                                    version=row["version"])
        # upsert_game owns the scrape columns only; the two this test is about
        # are written by the detail scraper, which is not running here.
        conn.execute("UPDATE games SET overview = ?, url = ? WHERE id = ?",
                     (row["overview"] or None, row["url"], gid))
        ids[row["slug"]] = gid
    conn.commit()
    conn.close()
    return ids


def check(label, condition, detail=""):
    print("%-46s %s %s" % (label, "OK" if condition else "FAIL", detail))
    if not condition:
        raise AssertionError(label)


def main():
    ids = seed()
    app = slg_gui.App(notify=False)
    app.geometry("1280x820")
    app.update()
    app.rows = slg_db.find_games(app.conn)
    by_id = {g["id"]: g for g in app.rows}

    # Switching to 中文 asks the translator for whatever is missing, and this
    # script runs against a throwaway database with no key - which resolves to
    # the free engine and a real HTTP call. Recorded instead of made, so the
    # checks below stay offline and deterministic and can still assert that the
    # request was asked for.
    asked = []
    app._overview_worker = lambda game: asked.append("overview")
    app._title_worker = lambda game: asked.append("title")

    # --- first selection -----------------------------------------------------
    app.select(by_id[ids["with-ov"]])
    app.update()
    p = app._detail_parts
    check("skeleton built", p is not None)
    check("title is the original", p["title"].cget("text") == WITH_OV["title"])
    check("overview shows the original",
          p["ov_label"].cget("text") == WITH_OV["overview"])
    check("url block is up", "url" in app._detail_shown)
    check("overview block is up", "ov_box" in app._detail_shown)
    check("cover is sized", p["cover"].cget("image") is not None)
    check("tag chips built", len(app._tag_chips) == 3)

    # --- rename --------------------------------------------------------------
    # Nothing has been translated yet, so there is nothing to correct: an empty
    # box, not the English name the user is not supposed to be editing.
    app._begin_title_edit()
    app.update()
    check("entry replaced the label", not p["title"].winfo_manager())
    check("entry is empty before a translation exists",
          p["title_entry"].get() == "")
    p["title_entry"].insert(0, "阿尔法冒险 v1.20 [Studio X]")
    app._save_title_edit()
    app.update()
    check("label is back", p["title"].winfo_manager() == "pack")
    check("label shows the new name",
          p["title"].cget("text") == "阿尔法冒险 v1.20 [Studio X]")
    check("db holds a manual row",
          slg_db.get_manual_translation(app.conn, "title", ids["with-ov"])
          == "阿尔法冒险 v1.20 [Studio X]")
    card = app._card_title.get(ids["with-ov"])
    check("card repainted", card is not None
          and card.cget("text") == "阿尔法冒险 v1.20 [Studio X]  v1.20")

    # an automatic translation landing must not overwrite the manual one
    wrote = slg_db.set_auto_translation(app.conn, "title", ids["with-ov"],
                                        WITH_OV["title"], "自动译名", engine="m")
    check("auto write refused", wrote is False)
    check("manual name survives",
          slg_db.get_manual_translation(app.conn, "title", ids["with-ov"])
          == "阿尔法冒险 v1.20 [Studio X]")

    # 原文 is the site's own text, so a correction to the translation steps
    # aside for it rather than replacing it
    app._set_overview_lang(by_id[ids["with-ov"]], "原文")
    app.update()
    check("原文 shows the original name",
          p["title"].cget("text") == WITH_OV["title"])
    app._set_overview_lang(by_id[ids["with-ov"]], "中文")
    app.update()
    check("中文 shows the hand-typed name",
          p["title"].cget("text") == "阿尔法冒险 v1.20 [Studio X]")
    check("the untranslated overview was asked for", asked == ["overview"], asked)
    asked.clear()

    # --- the editor opens on the Chinese, not the original -------------------
    slg_db.set_auto_translation(app.conn, "overview", ids["with-ov"],
                                WITH_OV["overview"], "你被困在一栋房子里。",
                                engine="m")
    app._begin_overview_edit()
    app.update()
    check("textbox replaced the label", not p["ov_label"].winfo_manager())
    check("textbox is prefilled with the translation",
          p["ov_text"].get("1.0", "end").strip() == "你被困在一栋房子里。")
    p["ov_text"].delete("1.0", "end")
    p["ov_text"].insert("1.0", "你被困在一栋房子里，出不去。")
    app._save_overview_edit()
    app.update()
    check("label is back", p["ov_label"].winfo_manager() == "pack")
    check("label shows the new text",
          p["ov_label"].cget("text") == "你被困在一栋房子里，出不去。")
    check("db holds a manual overview",
          slg_db.get_manual_translation(app.conn, "overview", ids["with-ov"])
          == "你被困在一栋房子里，出不去。")

    # --- clearing it hands the field back, with the translation intact -------
    # The whole point: correcting a translation must not cost the game the
    # translation, so the machine row has to still be there to fall back on.
    app._begin_overview_edit()
    p["ov_text"].delete("1.0", "end")
    app._save_overview_edit()
    app.update()
    check("cleared the manual overview",
          slg_db.get_manual_translation(app.conn, "overview", ids["with-ov"]) is None)
    check("the automatic translation came back",
          app._ov_label.cget("text") == "你被困在一栋房子里。")

    # --- a game with neither url nor overview --------------------------------
    app.select(by_id[ids["no-ov"]])
    app.update()
    check("url block went away", "url" not in app._detail_shown)
    check("overview block went away", "ov_box" not in app._detail_shown)
    check("title is the second game's", p["title"].cget("text") == NO_OV["title"])
    check("chips rebuilt", len(app._tag_chips) == 3)

    # ...and back again: the blocks have to return in the right order
    app.select(by_id[ids["with-ov"]])
    app.update()
    order = [w for w in app._detail_order if w[0] in app._detail_shown]
    keys = [w[0] for w in order]
    check("block order restored", keys == app._detail_shown, " ".join(keys))
    check("url is above the status row",
          keys.index("url") < keys.index("status") < keys.index("ov_box"))

    # --- the tag editor ------------------------------------------------------
    app.open_tag_editor()
    app.update()
    editor = app._tag_editor
    check("editor opened", editor is not None)
    entries = editor["entries"]
    check("every tag has a row", sorted(entries) == ["big-tits", "netorare", "story"])
    check("rows start empty", entries["netorare"].get() == "")

    slg_db.set_auto_translation(app.conn, "tag", "netorare", "netorare", "寝取られ",
                                engine="m")
    slg_gui.load_tag_translations(app.conn)
    entries["netorare"].delete(0, "end")
    entries["netorare"].insert(0, "NTR")
    entries["big-tits"].delete(0, "end")
    entries["big-tits"].insert(0, "巨乳")
    app._save_tag_edits()
    app.update()
    check("saved the tag", slg_db.get_manual_translation(app.conn, "tag", "netorare")
          == "NTR")
    check("overrode the automatic one",
          slg_gui.display_tag("netorare") == "NTR")
    check("card chip repainted",
          app._card_pool[0]["tagline"].cget("text").startswith("NTR"))
    check("entry normalised to the new value", entries["netorare"].get() == "NTR")

    # an automatic run must not take the hand-typed tag back
    wrote = slg_db.set_auto_translation(app.conn, "tag", "netorare", "netorare",
                                        "寝取られ", engine="m")
    check("auto tag write refused", wrote is False)
    check("hand-typed tag survived", slg_gui.display_tag("netorare") == "NTR")

    # A tag nobody edited must survive the clear button
    slg_db.set_auto_translation(app.conn, "tag", "story", "story", "剧情",
                                engine="m")
    slg_gui.load_tag_translations(app.conn)

    app._clear_tag_edits()
    app.update()
    check("manual tag cleared",
          slg_db.get_manual_translation(app.conn, "tag", "netorare") is None)
    check("cleared row falls back to the automatic translation",
          slg_gui.display_tag("netorare") == "寝取られ")
    check("untouched automatic tag kept",
          slg_gui.display_tag("story") == "剧情")

    editor["win"].destroy()
    app._tag_editor = None
    app.update()

    # --- refresh must not blank the panel ------------------------------------
    app.refresh()
    app.update()
    check("panel survived a refresh",
          p["title"].cget("text") == WITH_OV["title"])
    app._set_overview_lang(by_id[ids["with-ov"]], "中文")
    app.update()
    check("the hand-typed name survived too",
          p["title"].cget("text") == "阿尔法冒险 v1.20 [Studio X]")

    app.destroy()
    print("\nall smoke checks passed")


if __name__ == "__main__":
    main()
