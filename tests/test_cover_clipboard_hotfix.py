"""No real clipboard reads or writes; temporary images only."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

import slg_cover_clipboard as clipboard
import slg_local_library as library


class ClipboardCoverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "封面.png"

    def prepare(self, value):
        with patch.object(clipboard.ImageGrab, "grabclipboard", return_value=value):
            return clipboard.prepare_clipboard_cover()

    def test_bitmap_transparency_white_and_detached(self):
        source = Image.new("RGBA", (12, 16), (255, 0, 0, 0))
        result = self.prepare(source)
        self.addCleanup(result.close)
        self.assertEqual(result.mode, "RGB")
        self.assertEqual(result.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_bitmap_is_resized_to_shared_limits(self):
        result = self.prepare(Image.new("RGB", (2000, 3000)))
        self.addCleanup(result.close)
        self.assertEqual(result.size, (1600, 2400))

    def test_bitmap_pixel_limit_checked_before_load(self):
        source = Image.new("RGB", (10, 10))
        with patch.object(library, "MAX_COVER_PIXELS", 50), patch.object(source, "load") as load:
            with self.assertRaisesRegex(ValueError, "2500"):
                self.prepare(source)
            load.assert_not_called()

    def test_bitmap_encoded_limit(self):
        with patch.object(clipboard, "MAX_COVER_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "5 MB"):
                self.prepare(Image.new("RGB", (10, 10)))

    def test_file_list_uses_shared_decoder_and_detaches(self):
        with Image.new("RGB", (13, 17), "red") as source:
            source.save(self.path)
        result = self.prepare([str(self.path)])
        self.addCleanup(result.close)
        self.path.unlink()
        self.assertEqual(result.getpixel((0, 0)), (255, 0, 0))

    def test_file_exif_orientation(self):
        path = self.path.with_suffix(".jpg")
        with Image.new("RGB", (12, 20)) as source:
            exif = source.getexif()
            exif[274] = 6
            source.save(path, exif=exif)
        result = self.prepare([str(path)])
        self.addCleanup(result.close)
        self.assertEqual(result.size, (20, 12))

    def test_file_byte_limit(self):
        self.path.write_bytes(b"a" * 20)
        with patch.object(library, "MAX_COVER_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "5 MB"):
                self.prepare([str(self.path)])

    def test_unsupported_file_format(self):
        path = self.path.with_suffix(".gif")
        with Image.new("RGB", (10, 10)) as source:
            source.save(path)
        with self.assertRaisesRegex(ValueError, "JPEG"):
            self.prepare([str(path)])

    def test_corrupt_file(self):
        self.path.write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "损坏"):
            self.prepare([str(self.path)])

    def test_missing_file(self):
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.prepare([str(self.path)])

    def test_empty_text_and_urls_do_not_fetch(self):
        for value in (None, "https://example.invalid/cover.jpg", "some text", b"text"):
            with self.subTest(value=value), patch.object(clipboard, "prepare_cover") as decode:
                with self.assertRaisesRegex(ValueError, "没有可用图片"):
                    self.prepare(value)
                decode.assert_not_called()

    def test_multiple_or_no_files_rejected(self):
        for value in ([], ["a.png", "b.png"]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "只复制一个"):
                self.prepare(value)

    def test_network_relative_and_url_file_paths_rejected(self):
        for value in ("https://example.invalid/a.png", "//server/share/a.png", "\\\\server\\share\\a.png", "a.png"):
            with self.subTest(value=value), patch.object(clipboard.os.path, "isfile") as isfile:
                with self.assertRaisesRegex(ValueError, "本地图片"):
                    self.prepare([value])
                isfile.assert_not_called()

    def test_clipboard_unavailable_clear_error(self):
        with patch.object(clipboard.ImageGrab, "grabclipboard", side_effect=OSError("busy")):
            with self.assertRaisesRegex(ValueError, "重新复制"):
                clipboard.prepare_clipboard_cover()

    def test_prepare_does_not_save_or_change_override(self):
        with patch.object(library, "save_cover") as save, patch.object(library, "set_cover_override") as override:
            result = self.prepare(Image.new("RGB", (5, 5)))
            result.close()
            save.assert_not_called()
            override.assert_not_called()


if __name__ == "__main__":
    unittest.main()
