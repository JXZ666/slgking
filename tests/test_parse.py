"""Parser regressions.

Every case in here is a bug that actually happened against the live site and
was fixed; none of them are hypothetical. Run with:

    python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_scan  # noqa: E402
import slg_scrape  # noqa: E402


class ParseTitle(unittest.TestCase):
    def test_name_version_developer(self):
        self.assertEqual(
            slg_scrape.parse_title("Between Worlds [v0.2.1 Beta] [RolePlayer]"),
            ("Between Worlds", "0.2.1 Beta", "RolePlayer", 0))

    def test_status_bracket_is_glued_to_the_version_one(self):
        # The site writes '[v1.4.2 Beta][Final] [aura-dev]' with no space, so a
        # fixed two-bracket split puts the developer in the version slot.
        self.assertEqual(
            slg_scrape.parse_title(
                "Star Knightess Aura [v1.4.2 Beta][Final] [aura-dev]"),
            ("Star Knightess Aura", "1.4.2 Beta", "aura-dev", 1))

    def test_malformed_bracket(self):
        self.assertEqual(
            slg_scrape.parse_title("Some Game [Ep.7 Free]] [dev]"),
            ("Some Game", "Ep.7 Free", "dev", 0))

    def test_entities_decoded(self):
        # '&#8211;' left encoded broke the local-folder match.
        name, version, _, _ = slg_scrape.parse_title(
            "Horton Bay Stories &#8211; Jake [v1.0] [who]")
        self.assertEqual(name, "Horton Bay Stories \u2013 Jake")
        self.assertEqual(version, "1.0")

    def test_single_bracket_holding_a_version(self):
        # 'last group is the developer' filed this as written by v1.0.
        self.assertEqual(slg_scrape.parse_title("Game [v1.0]"),
                         ("Game", "1.0", None, 0))

    def test_single_bracket_holding_a_developer(self):
        self.assertEqual(slg_scrape.parse_title("Game [SomeDev]"),
                         ("Game", None, "SomeDev", 0))

    def test_plain_title(self):
        self.assertEqual(slg_scrape.parse_title("Plain Title"),
                         ("Plain Title", None, None, 0))

    def test_empty(self):
        self.assertEqual(slg_scrape.parse_title(""), ("", None, None, 0))
        self.assertEqual(slg_scrape.parse_title(None), ("", None, None, 0))


class ParseFolderVersion(unittest.TestCase):
    def test_platform_suffix(self):
        self.assertEqual(slg_scan.parse_folder_version("ParadiseCity-0.6.23-pc"),
                         "0.6.23")

    def test_r_prefix(self):
        self.assertEqual(slg_scan.parse_folder_version("Supower-Re0.69-pc"),
                         "0.69")

    def test_no_version(self):
        self.assertIsNone(slg_scan.parse_folder_version("多娜多娜"))


class FolderTitle(unittest.TestCase):
    def test_strips_version_and_noise(self):
        self.assertEqual(slg_scan.folder_title("ParadiseCity-0.6.23-pc"),
                         "ParadiseCity")

    def test_strips_chained_noise(self):
        self.assertEqual(slg_scan.folder_title("Some Game v1.0 Final Full Release"),
                         "Some Game")


class Key(unittest.TestCase):
    def test_ordinals_folded(self):
        # The site spells it 'Hikari: First Interlude', the folder
        # 'Hikari1stInterlude'.
        self.assertEqual(slg_scan._key("Hikari: First Interlude"),
                         slg_scan._key("Hikari1stInterlude"))

    def test_punctuation_dropped(self):
        self.assertEqual(slg_scan._key("Couples: Lustbound"),
                         slg_scan._key("couples lustbound"))


class Compare(unittest.TestCase):
    def test_ordering(self):
        self.assertEqual(slg_scan._compare("0.6.23", "0.6.9"), 1)
        self.assertEqual(slg_scan._compare("0.6.9", "0.6.23"), -1)
        self.assertEqual(slg_scan._compare("1.0", "1.0"), 0)

    def test_ragged_padding(self):
        self.assertEqual(slg_scan._compare("1.0", "1.0.0"), 0)

    def test_non_numeric_is_unknown(self):
        # 'Ep.7 Free' has digits, so it compares; pure text does not.
        self.assertEqual(slg_scan._compare("final", "beta"), 0)


class StripStyle(unittest.TestCase):
    def test_script_survives(self):
        # The rating lives in JSON-LD inside a <script>; stripping scripts is
        # what made the rating selector return zero matches.
        html = "<style>.x{color:red}</style><script>{\"ratingValue\":\"8.7\"}</script>"
        out = slg_scrape._strip_style(html)
        self.assertNotIn("color:red", out)
        self.assertIn("ratingValue", out)


# Shaped like the real markup: the title attribute carries name+version+dev,
# the cover is only in data-src (src is a lazy-load placeholder), and the tags
# live in the section's class attribute.
SECTION = """
<section class="gp-post-item tag-netorare tag-corruption platform-renpy category-windows">
  <a href="https://dikgames.com/between-worlds/" title="Between Worlds [v0.2.1 Beta] [RolePlayer]">
    <img src="data:image/gif;base64,R0lGOD" data-src="https://dikgames.com/wp-content/uploads/2026/01/cover-576x356.jpg">
  </a>
  <span class="gp-post-meta gp-meta-date"><time itemprop="datePublished" datetime="2026-07-25T10:00:00+00:00">July 25, 2026</time></span>
</section>
<section class="gp-post-item tag-corruption platform-renpy">
  <a href="https://dikgames.com/eva-ecstasy/" title="Eva&#8217;s Ecstasy [v1.4] [Someone]">
    <img src="data:image/gif;base64,R0lGOD" data-src="https://dikgames.com/wp-content/uploads/2026/02/eva-576x356.jpg">
  </a>
  <span class="gp-post-meta gp-meta-date"><time itemprop="datePublished" datetime="2026-08-01T10:00:00+00:00">August 1, 2026</time></span>
</section>
"""


class ParseListPage(unittest.TestCase):
    def test_extracts_every_field(self):
        games = slg_scrape.parse_list_page(SECTION)
        self.assertEqual(len(games), 2)

        first = games[0]
        self.assertEqual(first["slug"], "between-worlds")
        self.assertEqual(first["title"], "Between Worlds")
        self.assertEqual(first["version"], "0.2.1 Beta")
        self.assertEqual(first["developer"], "RolePlayer")
        self.assertEqual(first["engine"], "renpy")
        self.assertEqual(first["last_updated"], "2026-07-25")
        self.assertEqual(first["tags"], ["corruption", "netorare"])
        # data-src, never the base64 placeholder in src.
        self.assertEqual(
            first["cover_url"],
            "https://dikgames.com/wp-content/uploads/2026/01/cover-576x356.jpg")

    def test_entities_decoded_in_stored_title(self):
        # This is the one that raised "str has no attribute unescape": the
        # parameter was called 'html' and shadowed the module.
        games = slg_scrape.parse_list_page(SECTION)
        self.assertEqual(games[1]["title"], "Eva\u2019s Ecstasy")

    def test_empty_page(self):
        self.assertEqual(slg_scrape.parse_list_page(""), [])


if __name__ == "__main__":
    unittest.main()
