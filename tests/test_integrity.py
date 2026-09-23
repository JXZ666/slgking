"""账本封印：改档要做的是「门槛」，不是「保险箱」。

能保证的事情只有两件：
  1. 程序自己写的行，一律带得对上的封印；
  2. 程序之外塞进来 / 改过的行，后面的行就对不上链，程序能指出来并把它们排除
     出余额和头衔。

不能保证的是拦住 —— 密钥就在本机，想改的人抄下 integrity.key 重算整条链即可。
那也没关系：这是本地单机存档，防的是「随手用 DB 浏览器改一个数」。

这些测试全部用临时 LOCALAPPDATA，绝不碰用户真实存档。

Run with: `python -m unittest discover tests`
"""

import os
import shutil
import sqlite3
import tempfile
import unittest

import slg_db


class _TempHome(unittest.TestCase):
    """每个用例一个空的数据目录 —— 密钥、库文件都在里面。"""

    def setUp(self):
        self._old = os.environ.get("LOCALAPPDATA")
        self.tmp = tempfile.mkdtemp(prefix="slginteg_")
        os.environ["LOCALAPPDATA"] = self.tmp
        self.addCleanup(self._restore)
        self.conn = slg_db.connect()
        self.addCleanup(self.conn.close)

    def _restore(self):
        self.conn.close()
        if self._old is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reopen(self):
        """换个连接读同一个库 —— 绕开进程内的缓存，模拟「下次启动」。"""
        self.conn.close()
        self.conn = slg_db.connect()
        slg_db.invalidate_ledger(self.conn)
        return self.conn


class CleanLedger(_TempHome):
    def test_program_written_rows_verify(self):
        slg_db.add_points(self.conn, 30, "签到")
        slg_db.own_title(self.conn, "senior_user", "shop")
        self._reopen()
        self.assertEqual(slg_db.verify_ledger(self.conn),
                         {"sealed": True, "breaks": {}})
        self.assertIsNone(slg_db.tamper_report(self.conn))
        self.assertEqual(slg_db.points_balance(self.conn), 30)

    def test_reading_does_not_reseal_anything(self):
        slg_db.add_points(self.conn, 10, "签到")
        slg_db.points_balance(self.conn)
        slg_db.owned_title_ids(self.conn)
        self.assertEqual(slg_db.verify_ledger(self.conn)["breaks"], {})

    def test_empty_library_reports_no_tampering(self):
        self.assertEqual(slg_db.verify_ledger(self.conn),
                         {"sealed": False, "breaks": {}})
        self.assertIsNone(slg_db.tamper_report(self.conn))

    def test_integrity_key_is_not_in_the_database(self):
        slg_db.add_points(self.conn, 5, "签到")
        key = slg_db.integrity_key()
        self.assertTrue(key)
        with open(slg_db.db_path(), "rb") as fh:
            self.assertNotIn(key.encode("utf-8"), fh.read(),
                             "密钥混进了库文件，封印等于没上")


class HandEditedRows(_TempHome):
    def test_a_hand_written_row_is_excluded(self):
        slg_db.add_points(self.conn, 30, "签到")
        # DB 浏览器的一行 INSERT：没有封印，而且排在封印链之后。
        self.conn.execute("INSERT INTO points_log (delta, reason, at)"
                          " VALUES (99999, 'hand', 'x')")
        self.conn.commit()
        self._reopen()
        self.assertEqual(slg_db.points_balance(self.conn), 30)
        self.assertEqual(slg_db.tamper_report(self.conn),
                         {"points": 99999, "titles": []})

    def test_an_edited_amount_breaks_the_chain(self):
        slg_db.add_points(self.conn, 30, "签到")
        slg_db.add_points(self.conn, 5, "签到")
        self.conn.execute("UPDATE points_log SET delta = 99999 WHERE rowid = 1")
        self.conn.commit()
        self._reopen()
        self.assertNotEqual(slg_db.verify_ledger(self.conn)["breaks"], {})
        self.assertLess(slg_db.points_balance(self.conn), 99999)

    def test_deleting_a_middle_row_breaks_the_chain(self):
        for delta in (10, 20, 30):
            slg_db.add_points(self.conn, delta, "签到")
        self.conn.execute("DELETE FROM points_log WHERE rowid = 2")
        self.conn.commit()
        self._reopen()
        self.assertEqual(slg_db.verify_ledger(self.conn)["breaks"],
                         {"points_log": 3})

    def test_a_forged_title_is_excluded(self):
        slg_db.add_points(self.conn, 10, "签到")
        slg_db.own_title(self.conn, "group_friend", "code")
        self.conn.execute("INSERT INTO owned_titles (title_id, acquired_at,"
                          " source) VALUES ('butter_king', 'x', 'hand')")
        self.conn.commit()
        self._reopen()
        self.assertNotIn("butter_king", slg_db.owned_title_ids(self.conn))
        self.assertIn("group_friend", slg_db.owned_title_ids(self.conn))
        self.assertEqual(slg_db.tamper_report(self.conn),
                         {"points": 0, "titles": ["butter_king"]})

    def test_old_unsealed_rows_from_a_previous_version_stay_trusted(self):
        # 升级上来的存档：老行没有封印，链还没开始 —— 不该被当成改档。
        self.conn.execute("INSERT INTO points_log (delta, reason, at)"
                          " VALUES (40, 'old', 'x')")
        self.conn.commit()
        slg_db.add_points(self.conn, 10, "签到")
        self._reopen()
        self.assertEqual(slg_db.verify_ledger(self.conn)["breaks"], {})
        self.assertEqual(slg_db.points_balance(self.conn), 50)
        self.assertIsNone(slg_db.tamper_report(self.conn))


class MissingKey(_TempHome):
    def test_a_missing_key_disables_verification_instead_of_accusing(self):
        # 只拷了 slgking.db 没拷 integrity.key 的人：算不出封印，就不该冤枉他。
        slg_db.add_points(self.conn, 30, "签到")
        os.remove(slg_db.integrity_key_path())
        self._reopen()
        self.assertEqual(slg_db.verify_ledger(self.conn)["breaks"], {})
        self.assertIsNone(slg_db.tamper_report(self.conn))
        self.assertEqual(slg_db.points_balance(self.conn), 30)

    def test_verification_does_not_create_a_key(self):
        slg_db.add_points(self.conn, 30, "签到")
        os.remove(slg_db.integrity_key_path())
        slg_db.verify_ledger(self.conn)
        self.assertFalse(os.path.exists(slg_db.integrity_key_path()),
                         "只读的校验不该顺手生成密钥")


class ChainLimits(_TempHome):
    """把链做不到的事也钉住 —— 免得以后有人拿它当保险箱宣传。"""

    def test_a_perfect_restore_leaves_no_trace(self):
        # 封印是「内容 + 上一行封印」的哈希，改完又原样改回去，行内容与封印再次
        # 相符，链认不出来。效果上等于没改过，所以不算漏 —— 但边界就是这个：
        # 链只能发现**留下了改动**的存档。
        for delta in (10, 20, 30):
            slg_db.add_points(self.conn, delta, "签到")
        self.conn.execute("UPDATE points_log SET delta = 99999 WHERE rowid = 2")
        self.conn.commit()
        self.conn.execute("UPDATE points_log SET delta = 20 WHERE rowid = 2")
        self.conn.commit()
        self._reopen()
        self.assertEqual(slg_db.verify_ledger(self.conn)["breaks"], {})

    def test_a_whole_chain_can_be_reforged_by_whoever_holds_the_key(self):
        # 密钥在本机，抄走它重算整条链就能造出一份「干净」的存档。这是本地单机
        # 存档的天花板：防的是随手改一个数，不是防有心人。
        slg_db.add_points(self.conn, 30, "签到")
        key = slg_db.integrity_key()
        self.conn.execute("UPDATE points_log SET delta = 99999 WHERE rowid = 1")
        # 拿着密钥照 _row_seal 的规则把整条链重算一遍。
        prev = None
        for row in self.conn.execute(
                "SELECT rowid AS rid, delta, reason, at FROM points_log"
                " ORDER BY rowid").fetchall():
            values = (row["delta"], row["reason"], row["at"])
            prev = slg_db._row_seal(key, prev, row["rid"], values)
            self.conn.execute("UPDATE points_log SET seal = ? WHERE rowid = ?",
                              (prev, row["rid"]))
        self.conn.commit()
        self._reopen()
        self.assertEqual(slg_db.verify_ledger(self.conn)["breaks"], {})
        self.assertEqual(slg_db.points_balance(self.conn), 99999)


class BrokenChainIsSticky(_TempHome):
    def test_the_break_survives_a_process_restart(self):
        slg_db.add_points(self.conn, 30, "签到")
        self.conn.execute("INSERT INTO points_log (delta, reason, at)"
                          " VALUES (500, 'hand', 'x')")
        self.conn.commit()
        fresh = sqlite3.connect(slg_db.db_path())
        fresh.row_factory = sqlite3.Row
        self.addCleanup(fresh.close)
        self.assertNotEqual(slg_db.verify_ledger(fresh)["breaks"], {})


if __name__ == "__main__":
    unittest.main()
