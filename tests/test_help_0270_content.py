"""Help contract checks against actual export and source behavior, without Tk."""
import ast
import json
from pathlib import Path
import unittest

import slg_db

ROOT = Path(__file__).resolve().parents[1]


def candidate_source():
    source = (ROOT / "slg_gui.py").read_text(encoding="utf-8")
    edits = json.loads((ROOT / "docs/help_0270_replacements.json").read_text(encoding="utf-8"))
    for edit in edits["replacements"]:
        # Validate both the reviewable patch and the integrated GUI version.
        if edit["old"] in source:
            if source.count(edit["old"]) != 1:
                raise AssertionError("ambiguous replacement: " + edit["reason"])
            source = source.replace(edit["old"], edit["new"], 1)
        elif edit["new"] not in source:
            raise AssertionError("replacement drift: " + edit["reason"])
    return source


def help_texts():
    tree = ast.parse(candidate_source())
    return {node.name: "\n".join(child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str))
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_help_")}


class Help0270ContentTests(unittest.TestCase):
    def test_all_eighteen_sections_survive_reviewable_patch(self):
        tree = ast.parse(candidate_source())
        sections = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id == "HELP_SECTIONS"
                            for target in node.targets))
        names = [item[2] for item in ast.literal_eval(sections.value)]
        texts = help_texts()
        self.assertEqual(len(names), 18)
        for name in names:
            self.assertIn(name, texts)
            self.assertTrue(texts[name])

    def test_help_matches_json_export_privacy_boundary(self):
        conn = slg_db.connect(":memory:")
        try:
            game_id = slg_db.add_user_game(conn, "backup contract specimen")
            slug = conn.execute("SELECT slug FROM games WHERE id=?", (game_id,)).fetchone()[0]
            slg_db.add_comment(conn, slug, "PRIVATE_COMMENT_SENTINEL")
            slg_db.set_pref(conn, "translation.key", "SECRET_SENTINEL")
            backup = slg_db.export_user_data(conn)
            serialised = json.dumps(backup)
            self.assertNotIn("PRIVATE_COMMENT_SENTINEL", serialised)
            self.assertNotIn("SECRET_SENTINEL", serialised)
            self.assertIn("local_bindings", backup)
            self.assertIn("cover_overrides", backup)
            self.assertIn("hidden_games", backup)
            text = help_texts()["_help_backup"]
            self.assertIn("私人评论未纳入此JSON", text)
            self.assertIn("cloud_session.json", text)
            self.assertIn("cloud_admin_session.json", text)
        finally:
            conn.close()

    def test_help_does_not_promise_full_coverage_or_fixed_wait(self):
        texts = help_texts()
        joined = "\n".join(texts.values())
        for promise in ("北京时间 12:23", "通常不到一秒", "全量约 250 MB", "封面图跟着目录", "请求超时会在 8 秒内"):
            self.assertNotIn(promise, joined)
        self.assertIn("F95zone公开镜像", texts["_help_what"])
        self.assertIn("个人选图", texts["_help_sync"])
        self.assertIn("SLG大类和DLsite", texts["_help_sync"])
        self.assertIn("不再需要单独补图菜单", texts["_help_sync"])
        self.assertIn("缺少原站指标时保持未知", texts["_help_sync"])
        self.assertIn("确认", texts["_help_update"])
        self.assertIn("重启", texts["_help_update"])
        self.assertIn("不表示全站覆盖", texts["_help_dlsite"])

    def test_public_help_and_data_source_wording_do_not_advertise_private_source(self):
        # Inspect public presentation functions, not internal source IDs or
        # filtering logic that intentionally preserves provenance.
        tree = ast.parse(candidate_source())
        public = help_texts()
        sources = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.FunctionDef)
                       and node.name == "open_data_sources")
        public["open_data_sources"] = "\n".join(
            child.value for child in ast.walk(sources)
            if isinstance(child, ast.Constant) and isinstance(child.value, str))
        for name, text in public.items():
            self.assertNotIn("zyoudao", text.lower(), name)
        self.assertNotIn("VJ编号暂不支持", public["_help_scan"])


if __name__ == "__main__":
    unittest.main()
