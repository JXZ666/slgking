"""Offline real-Tk contrast checks; run separately from other Tk suites."""
import ast
import os
from pathlib import Path
import unittest

import customtkinter as ctk
from PIL import ImageGrab
import slg_gui
import test_client_0256_ui as fixture


def contrast(fg, bg):
    def luminance(value):
        values = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
                  for v in values]
        return sum(a * b for a, b in zip(values, (.2126, .7152, .0722)))
    a, b = sorted((luminance(fg), luminance(bg)))
    return (b + .05) / (a + .05)


class LightControls0270(fixture.Client0256UI):
    def assert_buttons_readable(self, parent):
        count = 0
        for button in self.widgets(parent):
            if not isinstance(button, ctk.CTkButton) or not button.cget('text'):
                continue
            if button.cget('state') == 'disabled':
                self.assertTrue(button.cget('text_color_disabled'))
            # Assert actual native label colors, including CTk's hover redraw.
            for hovered in (False, True):
                button._on_enter() if hovered else button._on_leave()
                label = button._text_label
                if label is not None:
                    def hex_color(color):
                        return '#' + ''.join('%02x' % (part // 257)
                            for part in label.winfo_rgb(color))
                    ratio = contrast(hex_color(label.cget('foreground')),
                                     hex_color(label.cget('background')))
                    self.assertGreaterEqual(ratio, 4.5,
                        (button.cget('text'), hovered, label.cget('foreground'),
                         label.cget('background'), ratio))
            count += 1
        self.assertGreater(count, 0)

    def test_library_popups_and_retained_theme_contrast(self):
        app = self.app
        app._apply_theme('light')
        app.set_library_view()
        self.settle()
        self.assert_buttons_readable(app)
        game = self.game()
        app.open_cover_editor(game)
        cover = app._cover_editor['win']
        app.open_local_binding(game)
        app.open_library_import()
        app.open_settings()
        app.open_help()
        app.deiconify()
        app.attributes('-topmost', True)
        app.geometry('1000x700')
        app.update_idletasks()
        self.assert_buttons_readable(app)
        output = os.environ.get('SLGKING_THEME_SCREENSHOT_DIR')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            app.lift()
            self.wait(lambda: app.winfo_ismapped() and app.winfo_width() >= 900)
            ready = []
            app.after(150, lambda: ready.append(True))
            self.wait(lambda: ready)
            # Only the isolated test window; never capture other desktop apps.
            x, y = app.winfo_rootx(), app.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + app.winfo_width(),
                                y + app.winfo_height())).save(
                Path(output) / 'light-library-dialogs.png')
        for mode in ('dark', 'light', 'system', 'light'):
            app._apply_theme(mode)
            self.settle()
            self.assertTrue(cover.winfo_exists())
            self.assert_buttons_readable(app)
        # Explicitly stress disabled / enabled without losing custom text colors.
        button = self.button(cover, '保存封面')
        self.assertEqual(button.cget('state'), 'disabled')
        button.configure(state='normal')
        self.assert_buttons_readable(cover)
        button.configure(state='disabled')

    def test_semantic_colors_have_contrast_in_both_modes(self):
        for palette in (slg_gui._LIGHT, slg_gui._DARK):
            for background in ('BG', 'CARD', 'CHIP', 'CARD_HOVER'):
                for foreground in ('TEXT', 'MUTED', 'ACCENT', 'DANGER_TEXT'):
                    self.assertGreaterEqual(contrast(palette[foreground], palette[background]),
                                            4.5, (foreground, background, palette))
            for background in ('ACCENT', 'ACCENT_HOVER'):
                self.assertGreaterEqual(contrast(palette['ON_ACCENT'], palette[background]), 4.5)

    def test_custom_surface_buttons_never_inherit_default_text(self):
        # Protect the construction sites that the real library fixture cannot reach.
        root = Path(slg_gui.__file__).parent
        for filename in ('slg_gui.py', 'slg_library_ui.py'):
            for node in ast.walk(ast.parse((root / filename).read_text(encoding='utf8'))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == 'ResourceButton'):
                    keywords = {kw.arg: kw.value for kw in node.keywords}
                    if 'fg_color' in keywords:
                        self.assertIn('text_color', keywords, (filename, node.lineno))
                        self.assertIn('text_color_disabled', keywords, (filename, node.lineno))
                        self.assertIn('hover_color', keywords, (filename, node.lineno))


if __name__ == '__main__':
    unittest.main()
