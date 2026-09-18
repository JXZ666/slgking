"""The AI translation layer.

Two things here are easy to get wrong and expensive to notice. The tag batch
must survive a reply that silently drops entries - 121 tags arriving as 118
translations looks like it worked. And the overview cache must go stale when
the site edits an overview, otherwise the library quietly serves a translation
of a description that no longer exists.

Nothing in here touches the network: `chat` is injected, so every test is a
plain function call.

    python -m unittest discover tests
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_engines  # noqa: E402
import slg_gui  # noqa: E402
import slg_translate as st  # noqa: E402


def _echo(messages, api_key, model, json_mode=False, timeout=None):
    """A model that translates every slug it was handed, cleanly."""
    asked = json.loads(messages[1]["content"])
    return json.dumps({slug: "译-" + slug for slug in asked}, ensure_ascii=False)


class ParseTagJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(st._parse_tag_json('{"a": "甲"}', ["a"]), {"a": "甲"})

    def test_markdown_fence(self):
        raw = '```json\n{"a": "甲", "b": "乙"}\n```'
        self.assertEqual(st._parse_tag_json(raw, ["a", "b"]),
                         {"a": "甲", "b": "乙"})

    def test_prose_around_the_object(self):
        raw = '好的，这是翻译结果：\n{"a": "甲"}\n希望有帮助。'
        self.assertEqual(st._parse_tag_json(raw, ["a"]), {"a": "甲"})

    def test_slugs_that_were_not_asked_for_are_dropped(self):
        # Tag names are database keys elsewhere; a model inventing one must not
        # be able to get it into the dictionary.
        raw = json.dumps({"a": "甲", "not-a-real-tag": "瞎编"})
        self.assertEqual(st._parse_tag_json(raw, ["a"]), {"a": "甲"})

    def test_unasked_keys_only_reply_is_empty(self):
        self.assertEqual(st._parse_tag_json('{"x": "1"}', ["a"]), {})

    def test_missing_keys_are_just_absent(self):
        self.assertEqual(st._parse_tag_json('{"a": "甲"}', ["a", "b"]),
                         {"a": "甲"})

    def test_non_string_and_blank_values_are_dropped(self):
        raw = '{"a": null, "b": "", "c": "   ", "d": 7, "e": "戊"}'
        self.assertEqual(st._parse_tag_json(raw, list("abcde")), {"e": "戊"})

    def test_values_are_stripped(self):
        self.assertEqual(st._parse_tag_json('{"a": "  甲  "}', ["a"]),
                         {"a": "甲"})

    def test_wrapped_mapping_is_unwrapped(self):
        raw = json.dumps({"translations": {"a": "甲"}})
        self.assertEqual(st._parse_tag_json(raw, ["a"]), {"a": "甲"})

    def test_garbage(self):
        for raw in ("", "   ", "not json at all", "{}", "[1, 2]", "{oops"):
            with self.subTest(raw=raw):
                self.assertEqual(st._parse_tag_json(raw, ["a"]), {})


class TagBatching(unittest.TestCase):
    def test_every_slug_is_translated(self):
        slugs = ["tag-%d" % i for i in range(121)]
        got = st.translate_tags(slugs, "k", chat=_echo)
        self.assertEqual(len(got), 121)
        self.assertEqual(sorted(got), sorted(slugs))

    def test_chunks_at_the_limit(self):
        slugs = ["tag-%d" % i for i in range(121)]
        seen = []

        def chat(messages, api_key, model, json_mode=False, timeout=None):
            asked = json.loads(messages[1]["content"])
            seen.append(len(asked))
            return json.dumps({s: "译" for s in asked})

        st.translate_tags(slugs, "k", chat=chat)
        self.assertEqual(seen, [40, 40, 40, 1])

    def test_a_dropped_entry_is_retried_on_its_own(self):
        # The failure mode that hides: the model answers 39 of 40 and the batch
        # looks fine. One tag would stay English forever.
        slugs = ["tag-%d" % i for i in range(78)]
        second = slugs[40:78]
        seen = []

        def chat(messages, api_key, model, json_mode=False, timeout=None):
            asked = json.loads(messages[1]["content"])
            seen.append(list(asked))
            if asked == second:
                return json.dumps({s: "译" for s in asked[:-1]})
            return json.dumps({s: "译" for s in asked})

        got = st.translate_tags(slugs, "k", chat=chat)
        self.assertEqual(len(got), 78)
        self.assertEqual([len(c) for c in seen], [40, 38, 1])
        self.assertEqual(seen[2], [second[-1]])

    def test_an_entry_the_model_never_returns_is_skipped_not_fatal(self):
        def chat(messages, api_key, model, json_mode=False, timeout=None):
            return json.dumps({"a": "甲"})

        got = st.translate_tags(["a", "b"], "k", chat=chat)
        self.assertEqual(got, {"a": "甲"})

    def test_logs_the_retry(self):
        lines = []
        st.translate_tags(["a"], "k", chat=lambda *a, **k: "{}",
                          log=lines.append)
        self.assertTrue(any("重试" in line for line in lines))


class OverviewAndProbe(unittest.TestCase):
    def test_overview_passes_the_translation_through(self):
        def chat(messages, api_key, model, json_mode=False, timeout=None):
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[1]["content"], "Hello there")
            return "  你好  "

        self.assertEqual(st.translate_overview("Hello there", "k", chat=chat),
                         "你好")

    def test_overview_rejects_an_empty_reply(self):
        with self.assertRaises(st.TranslateError):
            st.translate_overview("x", "k", chat=lambda *a, **k: "   ")

    def test_overview_does_not_ask_for_json_mode(self):
        # json_mode would make the model wrap the prose in an object.
        seen = {}

        def chat(messages, api_key, model, json_mode=False, timeout=None):
            seen["json_mode"] = json_mode
            return "译文"

        st.translate_overview("x", "k", chat=chat)
        self.assertFalse(seen["json_mode"])

    def test_overview_propagates_translate_error(self):
        def chat(*a, **k):
            raise st.TranslateError("HTTP 401")

        with self.assertRaises(st.TranslateError):
            st.translate_overview("x", "k", chat=chat)

    def test_probe_needs_a_key(self):
        with self.assertRaises(st.TranslateError):
            st.probe("", chat=_echo)

    def test_probe_uses_a_short_timeout(self):
        # Compared against the constant, not a literal: the point of the test is
        # that the probe is bounded by something short, and a hard-coded 20 is
        # how it silently stopped being short.
        seen = {}

        def chat(messages, api_key, model, json_mode=False, timeout=None):
            seen["timeout"] = timeout
            return "正常"

        self.assertEqual(st.probe("k", chat=chat), "正常")
        self.assertEqual(seen["timeout"], st.PROBE_TIMEOUT)
        self.assertLessEqual(st.PROBE_TIMEOUT, slg_engines.FREE_TIMEOUT)


class TitleTranslation(unittest.TestCase):
    """The game name.

    The rule the model is given is "translate the name, leave the version
    number and the studio alone" - and models are happy to obey that in spirit
    while rewriting v1.20 into 1.20版 or dropping it entirely. Since the title
    is what the user navigates the library by, a translation that fails the
    check is refused rather than shown.

    Refused, not replaced: this used to return the English source, and the
    caller cached whatever came back as though it were a translation - so the
    Chinese switch showed English, looked cached, and never retried. The
    exception is what makes "tried and impossible" a state the caller can
    record, separately from "translated".
    """

    def _chat(self, reply):
        def inner(messages, api_key, model, json_mode=False, timeout=None):
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[1]["content"], inner.asked)
            return reply
        return inner

    def _translate(self, source, reply):
        chat = self._chat(reply)
        chat.asked = source
        return st.translate_title(source, "k", chat=chat)

    def _refused(self, source, reply):
        with self.assertRaises(st.TitleGuardError):
            self._translate(source, reply)

    def test_translates_the_name(self):
        self.assertEqual(self._translate("Slutty Princess", "淫乱公主"),
                         "淫乱公主")

    def test_strips_quotes_the_model_adds_anyway(self):
        self.assertEqual(self._translate("Slutty Princess", '“淫乱公主”'),
                         "淫乱公主")

    def test_rejects_an_empty_reply(self):
        with self.assertRaises(st.TranslateError):
            self._translate("Slutty Princess", "   ")

    def test_rejects_the_name_handed_back_untouched(self):
        # Passing the English straight through used to satisfy the fragment
        # check trivially, so the cache filled with "translations" that were
        # the original text.
        self._refused("Slutty Princess", "Slutty Princess")
        self._refused("Slutty Princess", "  Slutty Princess  ")

    def test_a_reply_that_is_only_quotes_is_refused(self):
        self._refused("Slutty Princess", '""')

    def test_rejects_a_dropped_version_number(self):
        self._refused("Slutty Princess v1.20", "淫乱公主")

    def test_rejects_a_rewritten_version_number(self):
        self._refused("Slutty Princess v1.20", "淫乱公主 1.2")

    def test_rejects_a_dropped_studio_name(self):
        self._refused("Bloody Mary [StudioX]", "血腥玛丽")

    def test_bracketed_studio_name_survives_verbatim(self):
        self.assertEqual(self._translate("Bloody Mary [StudioX]",
                                         "血腥玛丽 [StudioX]"),
                         "血腥玛丽 [StudioX]")

    def test_a_bare_number_in_a_name_is_not_a_version(self):
        # "Princess 3" is part of the name, not a version. Flagging it would
        # make the guard throw away perfectly good translations.
        self.assertEqual(self._translate("Princess 3", "公主 3"), "公主 3")

    def test_rejects_a_dropped_dotted_number(self):
        self._refused("Princess 1.2.3", "公主")

    def test_rejects_dropped_prefixed_versions(self):
        self._refused("Princess EP03", "公主")
        self._refused("Princess Chapter 2", "公主")

    def test_the_refusal_names_the_guard_not_the_transport(self):
        # The caller writes a different cache marker for this than it does for
        # a dead key, so the two must stay distinguishable.
        self.assertTrue(issubclass(st.TitleGuardError, st.TranslateError))

    def test_fragments_finds_both_kinds(self):
        self.assertEqual(st.title_fragments("Bloody Mary [StudioX] v1.2"),
                         ["v1.2", "StudioX"])
        self.assertEqual(st.title_fragments(""), [])
        self.assertEqual(st.title_fragments(None), [])

    def test_does_not_ask_for_json_mode(self):
        # json_mode would make the model wrap the name in an object.
        seen = {}

        def chat(messages, api_key, model, json_mode=False, timeout=None):
            seen["json_mode"] = json_mode
            return "淫乱公主"

        st.translate_title("Slutty Princess", "k", chat=chat)
        self.assertFalse(seen["json_mode"])


class Cache(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.gid, _ = slg_db.upsert_game(self.conn, "a", "u/a", "A",
                                         tags=["netorare", "big-tits"])

    def tearDown(self):
        self.conn.close()

    def test_miss_then_hit(self):
        self.assertIsNone(slg_db.get_translation(self.conn, "overview", self.gid,
                                                 "原文"))
        slg_db.set_translation(self.conn, "overview", self.gid, "原文", "译文")
        self.assertEqual(
            slg_db.get_translation(self.conn, "overview", self.gid, "原文"),
            "译文")

    def test_edited_source_invalidates_the_row(self):
        slg_db.set_translation(self.conn, "overview", self.gid, "原文", "译文")
        self.assertIsNone(
            slg_db.get_translation(self.conn, "overview", self.gid, "改过的原文"))

    def test_set_overwrites_the_previous_value(self):
        slg_db.set_translation(self.conn, "overview", self.gid, "原文", "旧译文")
        slg_db.set_translation(self.conn, "overview", self.gid, "原文", "新译文")
        self.assertEqual(
            slg_db.get_translation(self.conn, "overview", self.gid, "原文"),
            "新译文")

    def test_hash_is_stable_and_handles_none(self):
        self.assertEqual(slg_db.src_hash("x"), slg_db.src_hash("x"))
        self.assertNotEqual(slg_db.src_hash("x"), slg_db.src_hash("y"))
        self.assertEqual(slg_db.src_hash(None), slg_db.src_hash(""))

    def test_missing_tags_excludes_what_is_cached(self):
        self.assertEqual(slg_db.missing_tags(self.conn), ["big-tits", "netorare"])
        slg_db.set_translation(self.conn, "tag", "netorare", "netorare", "NTR")
        self.assertEqual(slg_db.missing_tags(self.conn), ["big-tits"])

    def test_tag_translations_shape(self):
        self.assertEqual(slg_db.tag_translations(self.conn), {})
        slg_db.set_translation(self.conn, "tag", "netorare", "netorare", "NTR")
        self.assertEqual(slg_db.tag_translations(self.conn), {"netorare": "NTR"})

    def test_a_tag_translation_is_not_read_as_an_overview(self):
        slg_db.set_translation(self.conn, "tag", "1", "1", "NTR")
        self.assertIsNone(slg_db.get_translation(self.conn, "overview", "1", "1"))

    def test_a_title_round_trips_under_its_own_kind(self):
        # The title and the overview share one table and one ref (the game id),
        # so the kind column is the only thing keeping them apart.
        slg_db.set_translation(self.conn, "title", self.gid, "Princess", "公主")
        self.assertEqual(
            slg_db.get_translation(self.conn, "title", self.gid, "Princess"),
            "公主")
        self.assertIsNone(
            slg_db.get_translation(self.conn, "overview", self.gid, "Princess"))

    def test_an_edited_title_invalidates_the_row(self):
        slg_db.set_translation(self.conn, "title", self.gid, "Princess", "公主")
        self.assertIsNone(
            slg_db.get_translation(self.conn, "title", self.gid, "Princess 2"))

    def test_the_row_carries_the_engine_that_wrote_it(self):
        # get_translation_row is what the panel reads: the engine column is how
        # the "tried and impossible" marker is told apart from a translation.
        slg_db.set_translation(self.conn, "title", self.gid, "Princess", "公主",
                               engine="deepseek-chat")
        self.assertEqual(
            slg_db.get_translation_row(self.conn, "title", self.gid, "Princess"),
            ("公主", "deepseek-chat"))

    def test_the_row_is_none_when_the_source_changed(self):
        slg_db.set_translation(self.conn, "title", self.gid, "Princess", "公主",
                               engine="deepseek-chat")
        self.assertIsNone(
            slg_db.get_translation_row(self.conn, "title", self.gid, "Princess 2"))

    def test_the_untranslated_marker_round_trips(self):
        # The marker says "asked, and the name cannot be shown in Chinese".
        # Storing the source as the text is only half of it - the engine column
        # is what stops the next visit from paying for the same answer again.
        slg_db.set_translation(self.conn, "title", self.gid, "Princess", "Princess",
                               engine=slg_db.ENGINE_UNTRANSLATED)
        self.assertEqual(
            slg_db.get_translation_row(self.conn, "title", self.gid, "Princess"),
            ("Princess", slg_db.ENGINE_UNTRANSLATED))

    def test_title_translations_is_keyed_by_game_id(self):
        self.assertEqual(slg_db.title_translations(self.conn), {})
        slg_db.set_translation(self.conn, "title", self.gid, "Princess", "公主",
                               engine="deepseek-chat")
        self.assertEqual(slg_db.title_translations(self.conn),
                         {str(self.gid): ("公主", "deepseek-chat")})

    def test_title_translations_ignores_the_other_kinds(self):
        slg_db.set_translation(self.conn, "overview", self.gid, "原文", "译文")
        slg_db.set_translation(self.conn, "tag", "netorare", "netorare", "NTR")
        self.assertEqual(slg_db.title_translations(self.conn), {})

    def test_counts(self):
        self.assertEqual(slg_db.translation_counts(self.conn)["tag_total"], 2)
        slg_db.set_translation(self.conn, "tag", "netorare", "netorare", "NTR")
        self.assertEqual(slg_db.translation_counts(self.conn),
                         {"tags": 1, "tag_total": 2, "overviews": 0})


class ManualTranslations(unittest.TestCase):
    """A hand-written name/summary/tag has to survive the next sync and the next
    translation run. That is the whole point of letting the user type one."""

    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.gid, _ = slg_db.upsert_game(self.conn, "a", "u/a", "A",
                                         tags=["netorare"])

    def tearDown(self):
        self.conn.close()

    def test_a_manual_row_is_read_back(self):
        slg_db.set_manual_translation(self.conn, "title", self.gid, "公主")
        self.assertEqual(slg_db.get_manual_translation(self.conn, "title", self.gid),
                         "公主")
        self.assertEqual(
            slg_db.get_translation_row(self.conn, "title", self.gid, "Princess"),
            ("公主", slg_db.ENGINE_MANUAL))

    def test_a_manual_row_survives_the_source_changing(self):
        # The site editing an overview used to invalidate the row and send the
        # next visit back to the translator, which then overwrote the edit.
        slg_db.set_manual_translation(self.conn, "overview", self.gid, "手改")
        self.assertEqual(
            slg_db.get_translation(self.conn, "overview", self.gid, "完全不同的原文"),
            "手改")

    def test_an_automatic_write_will_not_clobber_a_manual_row(self):
        slg_db.set_manual_translation(self.conn, "title", self.gid, "公主")
        self.assertFalse(slg_db.set_auto_translation(self.conn, "title", self.gid,
                                                     "Princess", "机器译名",
                                                     engine="deepseek-chat"))
        self.assertEqual(
            slg_db.get_translation(self.conn, "title", self.gid, "Princess"), "公主")

    def test_an_automatic_write_still_lands_without_a_manual_row(self):
        self.assertTrue(slg_db.set_auto_translation(self.conn, "title", self.gid,
                                                    "Princess", "公主",
                                                    engine="deepseek-chat"))
        self.assertEqual(
            slg_db.get_translation_row(self.conn, "title", self.gid, "Princess"),
            ("公主", "deepseek-chat"))

    def test_a_manual_write_does_not_clobber_itself(self):
        slg_db.set_manual_translation(self.conn, "overview", self.gid, "第一版")
        slg_db.set_manual_translation(self.conn, "overview", self.gid, "第二版")
        self.assertEqual(
            slg_db.get_manual_translation(self.conn, "overview", self.gid), "第二版")

    def test_a_manual_write_does_not_destroy_the_machine_row(self):
        # The bug this whole split exists for: a correction used to be written
        # over the row it was correcting, so undoing the edit left nothing
        # behind and the game went back to English for good.
        slg_db.set_translation(self.conn, "title", self.gid, "Princess", "机器译名",
                               engine="deepseek-chat")
        slg_db.set_manual_translation(self.conn, "title", self.gid, "手改译名")
        self.assertEqual(
            slg_db.get_translation(self.conn, "title", self.gid, "Princess"), "手改译名")
        self.assertEqual(
            self.conn.execute("SELECT text FROM translations WHERE kind='title'"
                              ).fetchone()["text"], "机器译名")

    def test_clearing_a_manual_row_brings_the_machine_one_back(self):
        slg_db.set_translation(self.conn, "title", self.gid, "Princess", "机器译名",
                               engine="deepseek-chat")
        slg_db.set_manual_translation(self.conn, "title", self.gid, "手改译名")
        self.assertTrue(slg_db.delete_manual_translation(self.conn, "title", self.gid))
        self.assertIsNone(slg_db.get_manual_translation(self.conn, "title", self.gid))
        self.assertEqual(
            slg_db.get_translation(self.conn, "title", self.gid, "Princess"), "机器译名")

    def test_deleting_hands_the_field_back_to_the_translator(self):
        slg_db.set_manual_translation(self.conn, "title", self.gid, "公主")
        slg_db.delete_manual_translation(self.conn, "title", self.gid)
        self.assertIsNone(slg_db.get_manual_translation(self.conn, "title", self.gid))
        self.assertIsNone(
            slg_db.get_translation(self.conn, "title", self.gid, "Princess"))
        self.assertTrue(slg_db.set_auto_translation(self.conn, "title", self.gid,
                                                    "Princess", "公主",
                                                    engine="deepseek-chat"))

    def test_deleting_a_machine_row_cannot_reach_a_manual_one(self):
        slg_db.set_manual_translation(self.conn, "title", self.gid, "公主")
        slg_db.delete_translation(self.conn, "title", self.gid)
        self.assertEqual(slg_db.get_manual_translation(self.conn, "title", self.gid),
                         "公主")

    def test_get_manual_translation_ignores_machine_rows(self):
        slg_db.set_translation(self.conn, "title", self.gid, "Princess", "公主",
                               engine="deepseek-chat")
        self.assertIsNone(slg_db.get_manual_translation(self.conn, "title", self.gid))

    def test_a_manual_tag_stays_out_of_the_translation_queue(self):
        slg_db.set_manual_translation(self.conn, "tag", "netorare", "寝取")
        self.assertEqual(slg_db.missing_tags(self.conn), [])
        self.assertEqual(slg_db.tag_translations(self.conn), {"netorare": "寝取"})
        self.assertEqual(slg_db.translation_counts(self.conn)["tags"], 1)

    def test_a_tag_with_both_rows_is_counted_once(self):
        slg_db.set_auto_translation(self.conn, "tag", "netorare", "netorare", "寝取られ",
                                    engine="deepseek-chat")
        slg_db.set_manual_translation(self.conn, "tag", "netorare", "NTR")
        self.assertEqual(slg_db.translation_counts(self.conn)["tags"], 1)

    def test_the_two_tag_tables_merge_with_the_manual_one_winning(self):
        slg_db.set_translation(self.conn, "tag", "netorare", "netorare", "寝取られ",
                               engine="deepseek-chat")
        slg_db.set_manual_translation(self.conn, "tag", "netorare", "NTR")
        self.assertEqual(slg_db.tag_translations(self.conn), {"netorare": "NTR"})

    def test_all_tags_is_alphabetical(self):
        slg_db.upsert_game(self.conn, "b", "u/b", "B", tags=["bdsm", "anal"])
        self.assertEqual(slg_db.all_tags(self.conn), ["anal", "bdsm", "netorare"])

    def test_clearing_manual_rows_leaves_the_machine_ones(self):
        slg_db.set_manual_translation(self.conn, "tag", "netorare", "NTR")
        slg_db.set_auto_translation(self.conn, "tag", "bdsm", "bdsm", "BDSM",
                                    engine="deepseek-chat")
        slg_db.set_manual_translation(self.conn, "title", self.gid, "甲")
        self.assertEqual(slg_db.delete_manual_translations(self.conn, "tag"), 1)
        self.assertIsNone(
            slg_db.get_manual_translation(self.conn, "tag", "netorare"))
        self.assertEqual(slg_db.tag_translations(self.conn), {"bdsm": "BDSM"})
        # Scoped to the kind: the title edit is somebody else's button
        self.assertEqual(slg_db.get_manual_translation(self.conn, "title", self.gid),
                         "甲")

    def test_clearing_manual_rows_leaves_automatic_tags_re_translatable(self):
        slg_db.set_auto_translation(self.conn, "tag", "bdsm", "bdsm", "BDSM",
                                    engine="deepseek-chat")
        self.assertEqual(slg_db.delete_manual_translations(self.conn, "tag"), 0)
        self.assertEqual(slg_db.tag_translations(self.conn), {"bdsm": "BDSM"})


class RunTagTranslation(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        slg_db.upsert_game(self.conn, "a", "u/a", "A", tags=["netorare", "bdsm"])

    def tearDown(self):
        self.conn.close()

    def test_stores_what_the_model_returned(self):
        summary = st.run_tag_translation(self.conn, "k", chat=_echo)
        self.assertEqual(summary["translated"], 2)
        self.assertEqual(slg_db.tag_translations(self.conn),
                         {"bdsm": "译-bdsm", "netorare": "译-netorare"})

    def test_a_second_run_asks_for_nothing(self):
        st.run_tag_translation(self.conn, "k", chat=_echo)
        summary = st.run_tag_translation(
            self.conn, "k", chat=lambda *a, **k: self.fail("should not call out"))
        self.assertEqual(summary, {"pending": 0, "translated": 0, "missing": []})

    def test_limit_caps_the_batch(self):
        summary = st.run_tag_translation(self.conn, "k", limit=1, chat=_echo)
        self.assertEqual(summary["translated"], 1)
        self.assertEqual(len(slg_db.missing_tags(self.conn)), 1)

    def test_reports_slugs_the_model_withheld(self):
        summary = st.run_tag_translation(
            self.conn, "k", chat=lambda *a, **k: json.dumps({"bdsm": "BDSM"}))
        self.assertEqual(summary["missing"], ["netorare"])


class ResolveKey(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.was = os.environ.pop("DEEPSEEK_API_KEY", None)

    def tearDown(self):
        if self.was is not None:
            os.environ["DEEPSEEK_API_KEY"] = self.was
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self.conn.close()

    def test_explicit_wins(self):
        os.environ["DEEPSEEK_API_KEY"] = "from-env"
        slg_db.set_pref(self.conn, st.PREF_KEY, "from-prefs")
        self.assertEqual(st.resolve_key(self.conn, "explicit"), "explicit")

    def test_env_beats_the_settings_dialog(self):
        os.environ["DEEPSEEK_API_KEY"] = "from-env"
        slg_db.set_pref(self.conn, st.PREF_KEY, "from-prefs")
        self.assertEqual(st.resolve_key(self.conn), "from-env")

    def test_falls_back_to_prefs(self):
        slg_db.set_pref(self.conn, st.PREF_KEY, "from-prefs")
        self.assertEqual(st.resolve_key(self.conn), "from-prefs")

    def test_nothing_configured(self):
        self.assertIsNone(st.resolve_key(self.conn))


class DisplayTag(unittest.TestCase):
    def setUp(self):
        self.was = dict(slg_gui._TAG_ZH)
        slg_gui._TAG_ZH.clear()

    def tearDown(self):
        slg_gui._TAG_ZH.clear()
        slg_gui._TAG_ZH.update(self.was)

    def test_falls_back_to_the_hyphenated_slug(self):
        # The behaviour before any translation exists must not change.
        self.assertEqual(slg_gui.display_tag("big-tits"), "big tits")

    def test_prefers_the_translation(self):
        slg_gui._TAG_ZH["big-tits"] = "巨乳"
        self.assertEqual(slg_gui.display_tag("big-tits"), "巨乳")

    def test_an_empty_translation_falls_back(self):
        slg_gui._TAG_ZH["big-tits"] = ""
        self.assertEqual(slg_gui.display_tag("big-tits"), "big tits")

    def test_unknown_tag_falls_back(self):
        slg_gui._TAG_ZH["big-tits"] = "巨乳"
        self.assertEqual(slg_gui.display_tag("netorare"), "netorare")


class DisplayTitle(unittest.TestCase):
    """The card name, which has three states rather than two.

    A game whose name was translated shows Chinese; one that was never asked
    shows English; and one the guard refused also shows English - but the third
    is a recorded decision, not a pending one, so it must not look like a cache
    miss that a later run will fill in.
    """

    def setUp(self):
        self.was = dict(slg_gui._TITLE_ZH)
        slg_gui._TITLE_ZH.clear()
        self.game = {"id": 7, "title": "Slutty Princess"}

    def tearDown(self):
        slg_gui._TITLE_ZH.clear()
        slg_gui._TITLE_ZH.update(self.was)

    def test_untranslated_shows_the_source(self):
        self.assertEqual(slg_gui.display_title(self.game), "Slutty Princess")

    def test_translated_shows_chinese(self):
        slg_gui._TITLE_ZH["7"] = ("淫乱公主", "deepseek-chat")
        self.assertEqual(slg_gui.display_title(self.game), "淫乱公主")

    def test_the_marker_shows_the_source_not_the_stored_text(self):
        slg_gui._TITLE_ZH["7"] = ("Slutty Princess", slg_db.ENGINE_UNTRANSLATED)
        self.assertEqual(slg_gui.display_title(self.game), "Slutty Princess")

    def test_a_blank_translation_shows_the_source(self):
        slg_gui._TITLE_ZH["7"] = ("", None)
        self.assertEqual(slg_gui.display_title(self.game), "Slutty Princess")

    def test_another_game_is_not_picked_up(self):
        slg_gui._TITLE_ZH["8"] = ("淫乱公主", "deepseek-chat")
        self.assertEqual(slg_gui.display_title(self.game), "Slutty Princess")


if __name__ == "__main__":
    unittest.main()
