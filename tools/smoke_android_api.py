#!/usr/bin/env python
"""slg_android_api 桌面离线冒烟。

在临时数据目录上驱动进程内桥（bootstrap + 全部离线端点），不触碰真实
用户数据（%LOCALAPPDATA% 被替换为临时目录），不发任何网络请求（不启动
同步 job，只验证其错误路径）。任何一个断言失败即以非零码退出。

用法： py -3 tools/smoke_android_api.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_android_bootstrap  # noqa: E402
import slg_android_api  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_results = []


def call(method, **params):
    payload = dict(params, method=method)
    return json.loads(slg_android_api.call(json.dumps(payload, ensure_ascii=False)))


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       ("  -- " + str(detail)) if (detail and not cond) else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="slgking-android-smoke-")
    try:
        boot = slg_android_bootstrap.init(
            files_dir=tmp, asset_root=os.path.join(REPO, "assets"), crypto="stub")
        check("bootstrap.init", boot.get("ok") and boot.get("db_exists", True)
              and os.path.isfile(boot["db_path"]), boot)

        r = call("ping")
        check("ping", r["ok"] and r["bridge"] == slg_android_api.BRIDGE_VERSION, r)

        r = call("stats")
        check("stats", r["ok"] and r["games"] > 250 and r["tags"] > 50, r)
        games_total = call("catalog.list", catalog_id="western_slg")["total"]

        # 五种排序 × 两个方向：行数一致、首条符合排序键语义
        first = None
        for sort in ("score", "rating", "updated", "title", "heat"):
            for desc in (True, False):
                r = call("catalog.list", sort=sort, desc=desc, page=1, page_size=10)
                check("catalog.list %s/%s" % (sort, "desc" if desc else "asc"),
                      r["ok"] and r["total"] == games_total and len(r["items"]) == 10
                      and all(i["id"] != j["id"] for i, j in
                              zip(r["items"], r["items"][1:])), r.get("error"))
                if first is None:
                    first = r["items"][0]
        title_sorted = call("catalog.list", sort="title", desc=True, page_size=1)
        rating_sorted = call("catalog.list", sort="rating", desc=True, page_size=1)
        check("sort.rating orders",
              (rating_sorted["items"][0]["rating"] or -1) >=
              (title_sorted["items"][0]["rating"] or -1),
              (rating_sorted["items"][0]["rating"], title_sorted["items"][0]["rating"]))

        # 分页：第 1+2 页与一次性 20 条一致
        p1 = call("catalog.list", page=1, page_size=10)["items"]
        p2 = call("catalog.list", page=2, page_size=10)["items"]
        p20 = call("catalog.list", page=1, page_size=20)["items"]
        check("pagination", [x["id"] for x in p1 + p2] == [x["id"] for x in p20])

        # 搜索：取一个真实标题的前缀
        probe = first["title"][:6]
        r = call("catalog.list", q=probe)
        check("search", r["ok"] and 1 <= r["total"] <= games_total, (probe, r["total"]))

        # 详情
        r = call("game.get", id=first["id"])
        g = r.get("game") or {}
        check("game.get", r["ok"] and g.get("id") == first["id"]
              and isinstance(g.get("tags"), list) and "collections" in g, r.get("error"))

        # 评分：设 4 星 → 2 星 → 清除
        check("rate 4", call("game.rate", id=first["id"], stars=4)["my_rating"] == 4)
        check("rate 2", call("game.rate", id=first["id"], stars=2)["my_rating"] == 2)
        check("rate clear", call("game.rate", id=first["id"], stars=0)["my_rating"] is None)
        # 想玩视图：标 want 后可见，清除后消失
        call("game.state", id=first["id"], status="want")
        want = call("catalog.list", view="want")
        check("view want", any(i["id"] == first["id"] for i in want["items"]))
        call("game.state", id=first["id"], status="")
        want = call("catalog.list", view="want")
        check("view want cleared",
              not any(i["id"] == first["id"] for i in want["items"]))

        # 备注
        call("game.note", id=first["id"], note="手机端测试备注")
        check("note", call("game.get", id=first["id"])["game"]["note"] == "手机端测试备注")

        # 标签筛选：取计数最高的两个标签做交集/排除
        tags = call("tags.list", limit=200)["tags"]
        check("tags.list", len(tags) > 50 and all("count" in t and "zh" in t for t in tags))
        t1 = tags[0]["name"]
        r = call("catalog.list", tags=[t1])
        check("filter include", r["ok"] and 0 < r["total"] <= tags[0]["count"], (t1, r["total"]))
        if len(tags) > 1:
            r = call("catalog.list", exclude=[t1])
            check("filter exclude", r["ok"] and r["total"] < games_total, r["total"])

        # 收藏夹：建 → 收藏 → 收藏夹视图可见 → 重命名 → 删除
        c = call("collection.save", name="冒烟夹")
        check("collection.create", c["ok"] and c["id"], c)
        call("game.collections.set", id=first["id"], collection_ids=[c["id"]])
        listing = call("collections.list")["collections"]
        check("collections.list",
              any(x["id"] == c["id"] and x["count"] == 1 for x in listing), listing)
        r = call("catalog.list", view="collection", collection_id=c["id"])
        check("view collection", any(i["id"] == first["id"] for i in r["items"]))
        rn = call("collection.save", id=c["id"], name="冒烟夹2")
        check("collection.rename", rn["ok"] and rn["name"] == "冒烟夹2", rn)
        check("collection.delete", call("collection.delete", id=c["id"])["ok"])
        check("collections after delete",
              not any(x["id"] == c["id"] for x in
                      call("collections.list")["collections"]))

        # 我添加的游戏：增改删，origin=user 保护
        u = call("usergames.add", title="我的冒烟游戏", developer="tester",
                 tags=["3d-game"])
        check("usergames.add", u["ok"] and u["id"], u)
        gid = u["id"]
        check("usergames.update",
              call("usergames.update", id=gid, title="我的冒烟游戏2")["ok"])
        ug = call("catalog.list", view="user")
        check("usergames visible",
              any(i["id"] == gid and i["title"] == "我的冒烟游戏2" for i in ug["items"]))
        site_id = first["id"]
        check("usergames.delete guard",
              not call("usergames.delete", id=site_id)["ok"])
        check("usergames.delete", call("usergames.delete", id=gid)["ok"])

        # 账号/评论离线路径：未登录态、私人评论往返
        r = call("account.status")
        check("account.status logged out", r["ok"] and r["logged_in"] is False, r)
        r = call("comments.post", slug=first["slug"], content="私人测试评论",
                 public=False)
        check("private comment post", r["ok"] and r.get("local_id"), r)
        local_id = r.get("local_id")
        r = call("comments.page", slug=first["slug"])
        # 联网时返回真实页（该种子游戏通常无评论），断网时返回 offline 标记；
        # 两种形态都必须带本地私人评论
        check("comments page shape",
              r["ok"] and any(c["id"] == local_id for c in r.get("local_private", [])),
              r)
        check("private comment delete",
              call("comments.local_delete", id=local_id)["ok"])

        # 游戏化/工具离线路径
        r = call("shop.list")
        check("shop.list offline",
              r["ok"] and isinstance(r.get("items"), list) and len(r["items"]) > 3, r)
        r = call("lottery.status")
        check("lottery.status offline",
              r["ok"] and "paytable" in r and r["draws_today"] == 0, r)
        r = call("translate.settings")
        check("translate.settings", r["ok"] and "has_key" in r, r)
        r = call("points.ledger")
        check("points.ledger offline", r["ok"] and r.get("balance") == 0 and
              isinstance(r.get("ledger"), list), r)
        r = call("wardrobe.list")
        check("wardrobe.list logged-out guard", not r["ok"], r)

        # 想玩版本提醒往返：标想玩 → 建基线 → 抬版本 → 出变化 → 标记已看 → 清空
        probe2 = call("catalog.list", sort="title", desc=True, page_size=1)["items"][0]
        call("game.state", id=probe2["id"], status="want")
        r = call("wishlist.updates")
        check("wishlist baseline", r["ok"] and r["changes"] == [], r)
        r = call("wishlist.updates")
        check("wishlist stable", r["ok"] and r["changes"] == [], r)
        conn = __import__("slg_db").connect()
        orig_version = conn.execute(
            "SELECT version FROM games WHERE id=?", (probe2["id"],)).fetchone()["version"]
        conn.execute("UPDATE games SET version='v999.0' WHERE id=?", (probe2["id"],))
        conn.commit()
        conn.close()
        r = call("wishlist.updates")
        chg = r["changes"]
        check("wishlist change detected",
              r["ok"] and len(chg) == 1 and chg[0]["id"] == probe2["id"]
              and chg[0]["version"] == "v999.0", chg)
        check("wishlist mark seen",
              call("wishlist.mark.seen", id=probe2["id"])["ok"])
        r = call("wishlist.updates")
        check("wishlist cleared after seen", r["ok"] and r["changes"] == [], r)
        # 还原
        call("game.state", id=probe2["id"], status="")
        conn = __import__("slg_db").connect()
        conn.execute("UPDATE games SET version=? WHERE id=?", (orig_version, probe2["id"]))
        conn.execute("DELETE FROM wishlist_version_seen WHERE game_id=?", (probe2["id"],))
        conn.commit()
        conn.close()
        check("feedback validation",
              not call("feedback.submit", category="bug", content="太短")["ok"])

        # 错误路径：未知方法 / 缺参 / 未初始化的 sync.poll 不至于崩
        check("unknown method", not call("nope")["ok"])
        check("game.get missing", not call("game.get")["ok"])
        check("sync.poll no job", not call("sync.poll", job="sync-999")["ok"])
        bad = json.loads(slg_android_api.call("not json"))
        check("bad json", not bad["ok"], bad)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [x for x in _results if not x[1]]
    print("\n冒烟结果：%d/%d 通过" % (len(_results) - len(failed), len(_results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
