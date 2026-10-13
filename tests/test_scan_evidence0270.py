import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import slg_scan_evidence as evidence


class LocalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "random-folder"
        self.root.mkdir()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def test_known_metadata_without_directory_name(self):
        self.write("game/options.rpy", 'define config.name = _("Random Visual Novel")\n')
        self.write("data/System.json", '{"gameTitle":"魔法之旅"}')
        self.write("www/data/System.json", '{"gameTitle":"Alternate Game"}')
        self.write("Game.ini", '[Game]\nTitle=Another Title\n')
        result = evidence.extract_evidence(self.root)
        self.assertEqual({item["text"] for item in result["titles"]},
                         {"Random Visual Novel", "魔法之旅", "Alternate Game", "Another Title"})
        self.assertFalse(result["warnings"])
        self.assertTrue(all(not Path(item["path"]).is_absolute() for item in result["titles"]))

    def test_literal_only_never_evaluates_expression(self):
        self.write("game/options.rpy", 'define config.name = __import__("os").system("anything")\n')
        with patch("os.system", side_effect=AssertionError("executed")):
            self.assertFalse(evidence.extract_evidence(self.root)["titles"])

    def test_number_and_url_identity(self):
        numbered = self.root.parent / "[RJ01234567] game VJ123456"
        self.root.rename(numbered)
        self.root = numbered
        self.write("vndb.url", '[InternetShortcut]\nURL=https://vndb.org/v123?foo=bar\n')
        self.write("steam_appid.txt", "12345\n")
        self.write("RJ234567.exe", b"not an executable")
        with patch.object(evidence, "windows_executable_titles", return_value=[]):
            result = evidence.extract_evidence(self.root, ["RJ234567.exe"])
        self.assertEqual({(item["source"], item["external_id"]) for item in result["identities"]},
                         {("dlsite", "RJ01234567"), ("dlsite", "VJ123456"),
                          ("dlsite", "RJ234567"), ("vndb", "v123"), ("steam", "12345")})

    def test_version_not_identity_and_untrusted_urls(self):
        self.write("Game.ini", "[Game]\nTitle=Game v17\n")
        self.write("fake.url", "URL=https://evil.invalid/?https://vndb.org/v17\n")
        self.write("fakehost.url", "URL=https://vndb.org.evil.invalid/v17\n")
        self.assertFalse(evidence.extract_evidence(self.root)["identities"])

    def test_malformed_and_oversized_files_warn(self):
        self.write("data/System.json", "not-json")
        self.write("game/options.rpy", b"X" * (evidence.MAX_FILE_BYTES + 1))
        result = evidence.extract_evidence(self.root)
        self.assertFalse(result["titles"])
        self.assertEqual(len(result["warnings"]), 2)

    def test_generic_titles_filtered(self):
        self.write("Game.ini", "[Game]\nTitle=Game\n")
        self.write("play.exe", b"")
        with patch.object(evidence, "windows_executable_titles", return_value=[("Unity Player", "ProductName"), ("Actual Product", "FileDescription")]):
            result = evidence.extract_evidence(self.root, ["play.exe"])
        self.assertEqual([item["text"] for item in result["titles"]], ["Actual Product"])

    def test_helper_executables_do_not_pollute_evidence_or_consume_resources(self):
        for name in ("OpenSaveFolder.exe", "ResetConfig.exe", "Uninstaller.exe", "补丁安装程序.exe", "startup.exe"):
            self.write(name, b"")
        with patch.object(evidence, "windows_executable_titles", return_value=[("Actual Game", "ProductName")]) as reader:
            result = evidence.extract_evidence(self.root, ["OpenSaveFolder.exe", "ResetConfig.exe", "Uninstaller.exe", "补丁安装程序.exe", "startup.exe"])
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(result["titles"][0]["path"], "startup.exe")

    def test_helper_resource_labels_filtered_but_real_titles_kept(self):
        self.write("startup.exe", b"")
        values = [(name, "ProductName") for name in ("OpenSaveFolder", "ResetConfig", "Uninstaller", "Patchwork", "The Installer")]
        with patch.object(evidence, "windows_executable_titles", return_value=values):
            result = evidence.extract_evidence(self.root, ["startup.exe"])
        self.assertEqual([item["text"] for item in result["titles"]], ["Patchwork", "The Installer"])

    def test_alice_mixed_encodings_are_candidates_not_guessed_identity(self):
        self.write("AliceStart.ini", b"[AliceStart]\nGameName=" + "多娜多娜".encode("gb18030") + b"\nBootName=dohnadohna\nCodeName=../escape.exe\n")
        result = evidence.extract_evidence(self.root)
        self.assertFalse(result["identities"])
        names = result["titles"]
        self.assertTrue(any(item["text"] == "多娜多娜" and item["encoding"] == "gb18030" for item in names))
        self.assertTrue(any(item["text"] == "dohnadohna" and not item["ambiguous"] for item in names))
        self.assertFalse(any("escape" in item["text"] for item in names))

    def test_alice_unicode_and_malformed_safe_values(self):
        self.write("AliceStart.ini", '[AliceStart]\nGameName=ゲーム\nBootName="game.exe"\nCodeName=C:\\secret\\game.exe\n'.encode("utf-16"))
        result = evidence.extract_evidence(self.root)
        self.assertEqual([item["text"] for item in result["titles"]], ["ゲーム"])
        self.write("AliceStart.ini", b"GameName=\xff\nBootName=valid-name\nCodeName=a..b\n")
        result = evidence.extract_evidence(self.root)
        self.assertIn("valid-name", [item["text"] for item in result["titles"]])
        self.assertNotIn("a..b", [item["text"] for item in result["titles"]])

    def test_alice_code_file_yields_safe_stem_and_cancel_does_not_read(self):
        self.write("AliceStart.ini", b"CodeName=dohnadohna.ain\nBootName=dohnadohna.exe\n")
        result = evidence.extract_evidence(self.root)
        self.assertEqual([item["text"] for item in result["titles"]], ["dohnadohna"])
        with patch.object(Path, "open", side_effect=AssertionError("read after cancellation")):
            self.assertFalse(evidence.extract_evidence(self.root, should_stop=lambda: True)["titles"])

    def test_utf16_ini_and_percent_literal(self):
        self.write("Game.ini", "[Game]\nTitle=游戏100%\n".encode("utf-16"))
        self.assertEqual(evidence.extract_evidence(self.root)["titles"][0]["text"], "游戏100%")

    def test_cancel_before_any_io(self):
        with patch.object(Path, "open", side_effect=AssertionError("read after cancel")):
            result = evidence.extract_evidence(self.root, should_stop=lambda: True)
        self.assertEqual(result, {"identities": [], "titles": [], "warnings": ["识别已取消"]})

    def test_cancel_during_scan(self):
        self.write("Game.ini", "[Game]\nTitle=Recognized\n")
        calls = [0]
        def stop():
            calls[0] += 1
            return calls[0] > 3
        result = evidence.extract_evidence(self.root, should_stop=stop)
        self.assertIn("识别已取消", result["warnings"])
        self.assertFalse(result["titles"])

    def test_maximum_eight_resources(self):
        for index in range(15):
            self.write(f"{index:02}.url", f"URL=https://vndb.org/v{index + 1}\n")
        self.assertEqual(len(evidence.extract_evidence(self.root)["identities"]), 8)

    def test_external_executable_rejected(self):
        outside = Path(self.temp.name) / "RJ123456.exe"
        outside.write_bytes(b"")
        with patch.object(evidence, "windows_executable_titles", side_effect=AssertionError("read outside")):
            result = evidence.extract_evidence(self.root, [outside])
        self.assertFalse(result["identities"])
        self.assertTrue(result["warnings"])

    def test_missing_folder_reports_warning(self):
        result = evidence.extract_evidence(self.root / "absent")
        self.assertTrue(result["warnings"])

    def test_permission_failure_does_not_hide_other_metadata(self):
        self.write("data/System.json", '{"gameTitle":"Blocked"}')
        self.write("Game.ini", "[Game]\nTitle=Readable\n")
        original_open = Path.open
        def guarded(path, *args, **kwargs):
            if path.name == "System.json":
                raise PermissionError("denied")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            result = evidence.extract_evidence(self.root)
        self.assertEqual([item["text"] for item in result["titles"]], ["Readable"])
        self.assertIn("PermissionError", result["warnings"][0])

    def test_invalid_resource_warns_without_losing_executable_number(self):
        self.write("RJ123456.exe", b"")
        with patch.object(evidence, "windows_executable_titles", side_effect=ValueError("版本信息超过读取上限")):
            result = evidence.extract_evidence(self.root, ["RJ123456.exe"])
        self.assertEqual(result["identities"][0]["external_id"], "RJ123456")
        self.assertTrue(result["warnings"])

    def test_nested_shortcuts_not_scanned(self):
        self.write("internal/deeper/site.url", "URL=https://vndb.org/v17\n")
        self.assertFalse(evidence.extract_evidence(self.root)["identities"])

    def test_link_metadata_not_followed(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text('{"gameTitle":"Wrong"}')
        link = self.root / "data"
        try:
            link.symlink_to(Path(self.temp.name), target_is_directory=True)
        except OSError:
            self.skipTest("symlink permission unavailable")
        (Path(self.temp.name) / "System.json").write_text('{"gameTitle":"Wrong"}')
        result = evidence.extract_evidence(self.root)
        self.assertFalse(result["titles"])
        self.assertTrue(result["warnings"])


if __name__ == "__main__":
    unittest.main()
