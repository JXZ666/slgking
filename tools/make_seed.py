"""Build the bundled seed library from the author's own database.

Run from a normal terminal (not under CodePilot, whose LOCALAPPDATA is
shadowed) before packaging a release:

    python tools/make_seed.py

It copies the top N games by heat - only ones that already have a cover - out
of the author's populated db into assets/seed/, as a clean database with no
user state, ratings, or translations. On a fresh install the GUI copies this
into %LOCALAPPDATA%\\slgking, so a first-time user has something to browse
before their first sync.

Rows keep their original ids, so the game_tags foreign keys survive the copy.
"""

import argparse
import os
import sqlite3
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_seed(src_path, covers_src, out_path, covers_out, count):
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    picked = src.execute(
        "SELECT id, cover_file FROM games"
        " WHERE cover_file IS NOT NULL"
        " ORDER BY COALESCE(heat, 0) DESC, COALESCE(site_views, 0) DESC"
        " LIMIT ?", (count,)).fetchall()
    src.close()
    if not picked:
        print("源库里没有带封面的游戏，中止")
        return 1
    ids = [r["id"] for r in picked]
    marks = ",".join("?" * len(ids))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    dst.executescript(slg_db.SCHEMA)
    dst.execute("ATTACH DATABASE ? AS src", (src_path,))
    dst.execute("INSERT INTO games SELECT * FROM src.games WHERE id IN (%s)"
                % marks, ids)
    dst.execute(
        "INSERT INTO tags (id, name) SELECT DISTINCT t.id, t.name"
        " FROM src.tags t JOIN src.game_tags gt ON gt.tag_id = t.id"
        " WHERE gt.game_id IN (%s)" % marks, ids)
    dst.execute("INSERT INTO game_tags SELECT * FROM src.game_tags"
                " WHERE game_id IN (%s)" % marks, ids)

    # Covers are bundled into a onefile exe that unpacks them to %TEMP% on every
    # launch, so their total size adds to startup time on top of the onefile
    # format's own unpack cost. The source covers (576x356) are only ever shown
    # as 128x79 cards / 300x185 detail, so shrink them to a 384px JPEG instead
    # of shipping the originals verbatim.
    os.makedirs(covers_out, exist_ok=True)
    # Covers are re-encoded to .jpg below; a previous run's .webp/.avif/.jpeg
    # files would otherwise survive as stale full-size bloat in the bundle.
    for stale in os.listdir(covers_out):
        os.remove(os.path.join(covers_out, stale))
    copied = 0
    for r in picked:
        cover = os.path.join(covers_src, r["cover_file"])
        out_name = os.path.splitext(r["cover_file"])[0] + ".jpg"
        if os.path.exists(cover):
            try:
                img = Image.open(cover).convert("RGB")
                img.thumbnail((384, 384), Image.LANCZOS)
                img.save(os.path.join(covers_out, out_name), "JPEG", quality=72)
            except Exception:  # noqa: BLE001 - one bad cover must not kill the build
                continue
            dst.execute("UPDATE games SET cover_file = ? WHERE id = ?",
                        (out_name, r["id"]))
            copied += 1
    dst.commit()
    dst.close()
    os.replace(tmp, out_path)

    print("seed 库写入 %s（%d 款，%d 张封面）" % (out_path, len(picked), copied))
    return 0


def main():
    ap = argparse.ArgumentParser(description="从作者库导出 seed 数据")
    ap.add_argument("--src", default=slg_db.db_path(), help="源数据库路径")
    ap.add_argument("--covers-src", default=slg_db.covers_dir(),
                    help="源封面目录路径")
    ap.add_argument("--count", type=int, default=300, help="导出游戏数量")
    ap.add_argument("--out", default=os.path.join(HERE, "assets", "seed",
                                                  "slgking.db"))
    ap.add_argument("--covers-out", default=os.path.join(HERE, "assets", "seed",
                                                         "covers"))
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print("找不到源库：%s（用 --src 指定）" % args.src)
        return 1
    return build_seed(args.src, args.covers_src, args.out, args.covers_out,
                      args.count)


if __name__ == "__main__":
    sys.exit(main())
