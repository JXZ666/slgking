import unittest

from slg_sidebar_layout import (
    COMPACT, NORMAL, LARGE, SidebarLayout,
    sidebar_layout, sidebar_layout_with_hysteresis,
)


class SidebarLayoutTests(unittest.TestCase):
    def test_exact_thresholds_and_required_dimensions(self):
        for width, height, expected in (
            (1099, 1200, COMPACT), (2000, 799, COMPACT),
            (1100, 800, NORMAL), (1599, 1200, NORMAL),
            (2000, 949, NORMAL), (1600, 950, LARGE),
        ):
            with self.subTest(width=width, height=height):
                self.assertEqual(sidebar_layout(width, height), expected)
                self.assertEqual(sidebar_layout_with_hysteresis(width, height, None), expected)
        self.assertEqual((COMPACT.width, COMPACT.row_height, COMPACT.font_size), (190, 32, 13))
        self.assertEqual((NORMAL.width, NORMAL.row_height, NORMAL.font_size), (206, 38, 14))
        self.assertEqual((LARGE.width, LARGE.row_height, LARGE.font_size), (224, 44, 15))

    def test_compact_hysteresis_both_dimensions(self):
        choose = sidebar_layout_with_hysteresis
        self.assertEqual(choose(1123, 1000, COMPACT), COMPACT)
        self.assertEqual(choose(2000, 823, COMPACT), COMPACT)
        self.assertEqual(choose(1124, 824, COMPACT), NORMAL)
        self.assertEqual(choose(1076, 776, NORMAL), NORMAL)
        self.assertEqual(choose(1075, 1000, NORMAL), COMPACT)
        self.assertEqual(choose(2000, 775, NORMAL), COMPACT)

    def test_large_hysteresis_both_dimensions(self):
        choose = sidebar_layout_with_hysteresis
        self.assertEqual(choose(1623, 1200, NORMAL), NORMAL)
        self.assertEqual(choose(2000, 973, NORMAL), NORMAL)
        self.assertEqual(choose(1624, 974, NORMAL), LARGE)
        self.assertEqual(choose(1576, 926, LARGE), LARGE)
        self.assertEqual(choose(1575, 1200, LARGE), NORMAL)
        self.assertEqual(choose(2000, 925, LARGE), NORMAL)

    def test_large_jumps_and_zero_margin(self):
        choose = sidebar_layout_with_hysteresis
        self.assertEqual(choose(2000, 1200, COMPACT), LARGE)
        self.assertEqual(choose(900, 700, LARGE), COMPACT)
        for width, height in ((1100, 800), (1600, 950), (1099, 799)):
            for old in (COMPACT, NORMAL, LARGE):
                self.assertEqual(choose(width, height, old, margin=0), sidebar_layout(width, height))
        with self.assertRaises(ValueError):
            choose(1200, 900, NORMAL, margin=-1)
        with self.assertRaises(ValueError):
            choose(1200, 900, SidebarLayout("unknown", 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
