"""Separate-process real Tk verification; all writes use a temporary profile.

The real game folder is read only. Providers are mocked, and no game is run.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import customtkinter as ctk
import slg_db
import slg_scan
import slg_scan_evidence
import slg_library_metadata as metadata
import slg_library_providers as providers
from test_library_queries0270_ui import LibraryQueriesUI

REPORT = {}
GAME_FOLDER = Path("D:/game&novel/黄油/Galgame/多娜多娜 一起干坏事吧")


class FormalMatching(LibraryQueriesUI):
    def test_formal_matching_real_controls(self):
        evidence = slg_scan_evidence.extract_evidence(
            GAME_FOLDER, [p for p in GAME_FOLDER.iterdir() if p.suffix.lower() == ".exe"])
        names = [item["text"] for item in evidence["titles"]]
        self.assertFalse(any(slg_scan_evidence.auxiliary_title(name) for name in names))
        item = {"name": GAME_FOLDER.name, "path": str(GAME_FOLDER), "evidence": evidence}
        captured = []
        self.app.open_metadata_query(item, lambda facts: captured.append(facts))
        dialog = self.app._metadata_query_dialog
        self.app.update_idletasks()
        self.assertEqual(dialog["query"].get(), GAME_FOLDER.name)
        menus = [w for w in self.widgets(dialog["win"]) if isinstance(w, ctk.CTkOptionMenu)]
        values = [v for menu in menus for v in menu.cget("values")]
        for hint in evidence["titles"]:
            if hint.get("ambiguous"):
                self.assertTrue(any(hint["text"] in v and hint["encoding"] in v and "请核对" in v for v in values))
        facts = self.facts("v27519", title="doHna:doHna -Issho ni Warui Koto o Shiyou-",
                           original_title="ドーナドーナ", aliases=["多娜多娜", "Dohna Dohna"])
        before = self.count_games()
        with patch.object(providers, "search", return_value=[facts]) as search, \
             patch.object(providers, "details", return_value=facts) as details:
            self.assertFalse(search.called)
            dialog["search"]()
            self.wait(lambda: not dialog["state"]["busy"])
            self.assertEqual(search.call_args.args, ("vndb", GAME_FOLDER.name))
            self.assertFalse(details.called)
            self.assertIsNone(dialog["state"]["candidate"])
            dialog["confirm"]()
            self.assertFalse(captured)
            self.assertEqual(self.count_games(), before)
            dialog["pick"](facts)
            self.wait(lambda: not dialog["state"]["busy"])
            self.assertEqual(self.count_games(), before)
            dialog["confirm"]()
        self.assertEqual(len(captured), 1)
        self.assertIn(GAME_FOLDER.name, captured[0]["aliases"])
        gid = metadata.confirm(self.app.conn, captured[0])
        self.app.conn.commit()
        db_path = self.app.conn.execute("PRAGMA database_list").fetchone()[2]
        with slg_db.connect(db_path) as reopened:
            aliases = [r[0] for r in reopened.execute("SELECT alias FROM game_aliases WHERE game_id=?", (gid,))]
            self.assertIn(GAME_FOLDER.name, aliases)
            scan = slg_scan.scan(reopened, roots=[str(GAME_FOLDER)], mode="single", apply=False, log=lambda _: None)
            self.assertEqual(scan["items"][0]["game_id"], gid)
            self.assertEqual(scan["items"][0]["status"], "matched")
            repeated = metadata.confirm(reopened, captured[0])
            self.assertEqual(repeated, gid)
            self.assertEqual(reopened.execute("SELECT count(*) FROM personal_game_metadata WHERE source='vndb' AND external_id='v27519'").fetchone()[0], 1)
        self.assertFalse(self.callback_errors)
        REPORT.update(default_query=GAME_FOLDER.name, title_choices=values, evidence=evidence,
                      provider="offline mock", confirmed_aliases=aliases,
                      reopened_scan={k: scan["items"][0][k] for k in ("status", "game_id", "reason")},
                      callback_errors=0, game_executed=False, user_profile_written=False)


if __name__ == "__main__":
    suite = unittest.TestSuite([FormalMatching("test_formal_matching_real_controls")])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    REPORT["passed"] = result.wasSuccessful()
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".claude/formal-matching-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output))
    raise SystemExit(0 if result.wasSuccessful() else 1)
