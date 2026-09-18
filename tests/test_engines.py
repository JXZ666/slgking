"""The transport layer, and the capability flags the rest of the app trusts.

Two things here decide behaviour far away from this file. The `can_json` flag
is what keeps a machine translator away from the tag dictionary at all - if it
ever came back True for the free engine the guard would not fire and 120 terms
would quietly get worse. And `resolve_config` is where an install that predates
the engine switch either keeps working or silently stops: a missing provider
has to mean DeepSeek, because that is what every old install talked to.

No test here touches the network. `urlopen` is replaced, so a real HTTP error
is whatever object the test hands back.

    python -m unittest discover tests
"""

import gzip
import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_engines as se  # noqa: E402


def _completion(text):
    return {"choices": [{"message": {"content": text}}]}


class _Response:
    """Enough of an http.client.HTTPResponse for _fetch."""

    def __init__(self, body, encoding=None, raw=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self._body = body
        self.headers = {"Content-Encoding": encoding} if encoding else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body=""):
    return urllib.error.HTTPError(
        "https://example.invalid", code, "err", None,
        io.BytesIO(body.encode("utf-8") if isinstance(body, str) else body))


class _Transport:
    """Stands in for urlopen, and remembers every request it was handed.

    A reply that is an exception is raised instead of returned, which is how
    the fallback chains below are driven.
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        if not self.replies:
            raise AssertionError("unexpected request to %s" % req.full_url)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        # A callable stands in for a request that never comes back, which is
        # what a blackholed DNS resolver looks like from here.
        if callable(reply):
            reply = reply()
        return reply

    def __enter__(self):
        self.timeouts = []
        self._patch = mock.patch("urllib.request.urlopen", self)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False

    def body(self, index=0):
        return json.loads(self.requests[index].data.decode("utf-8"))

    def url(self, index=0):
        return self.requests[index].full_url


class OpenAIEngineTransport(unittest.TestCase):
    def test_the_base_url_gets_the_endpoint_appended(self):
        with _Transport(_Response(_completion("x"))) as t:
            se.OpenAIEngine("https://api.deepseek.com").chat([], "k", "m")
        self.assertEqual(t.url(), "https://api.deepseek.com/chat/completions")

    def test_a_trailing_slash_does_not_double_up(self):
        with _Transport(_Response(_completion("x"))) as t:
            se.OpenAIEngine("https://api.example.com/v1/").chat([], "k", "m")
        self.assertEqual(t.url(), "https://api.example.com/v1/chat/completions")

    def test_every_preset_that_has_a_base_url_joins_cleanly(self):
        # The presets are data, so the only way to catch a typo in one is to
        # walk them. `custom` ships empty on purpose and is skipped.
        for pid, _, base_url, _model in se.PROVIDERS:
            if not base_url:
                continue
            with self.subTest(provider=pid):
                with _Transport(_Response(_completion("x"))) as t:
                    se.OpenAIEngine(base_url).chat([], "k", "m")
                self.assertEqual(t.url(), base_url.rstrip("/") + "/chat/completions")

    def test_the_key_rides_in_the_authorization_header(self):
        with _Transport(_Response(_completion("x"))) as t:
            se.OpenAIEngine("https://api.deepseek.com").chat([], "sk-abc", "m")
        self.assertEqual(t.requests[0].get_header("Authorization"), "Bearer sk-abc")

    def test_json_mode_asks_the_provider_for_an_object(self):
        with _Transport(_Response(_completion("{}"))) as t:
            se.OpenAIEngine("https://api.deepseek.com").chat([], "k", "m", True)
        self.assertEqual(t.body()["response_format"], {"type": "json_object"})

    def test_json_mode_is_absent_by_default(self):
        with _Transport(_Response(_completion("x"))) as t:
            se.OpenAIEngine("https://api.deepseek.com").chat([], "k", "m")
        self.assertNotIn("response_format", t.body())

    def test_the_reply_text_comes_back(self):
        with _Transport(_Response(_completion("你好"))):
            got = se.OpenAIEngine("https://api.deepseek.com").chat([], "k", "m")
        self.assertEqual(got, "你好")

    def test_a_gzipped_body_is_decoded(self):
        packed = gzip.compress(json.dumps(_completion("压缩")).encode("utf-8"))
        with _Transport(_Response(packed, encoding="gzip")):
            got = se.OpenAIEngine("https://api.deepseek.com").chat([], "k", "m")
        self.assertEqual(got, "压缩")

    def test_a_gzipped_body_is_decoded_without_the_header_too(self):
        # Some proxies strip Content-Encoding and leave the bytes alone. The
        # gzip magic number is the tell.
        packed = gzip.compress(json.dumps(_completion("压缩")).encode("utf-8"))
        with _Transport(_Response(packed)):
            got = se.OpenAIEngine("https://api.deepseek.com").chat([], "k", "m")
        self.assertEqual(got, "压缩")

    def test_the_providers_own_message_survives_an_http_error(self):
        with _Transport(_http_error(401, '{"error":{"message":"Authentication Fails"}}')):
            with self.assertRaises(se.TranslateError) as caught:
                se.OpenAIEngine("https://api.deepseek.com").chat([], "bad", "m")
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertIn("Authentication Fails", str(caught.exception))

    def test_an_unreachable_host_reads_as_a_network_error(self):
        with _Transport(urllib.error.URLError("getaddrinfo failed")):
            with self.assertRaises(se.TranslateError) as caught:
                se.OpenAIEngine("https://api.deepseek.com").chat([], "k", "m")
        self.assertTrue(str(caught.exception).startswith("网络错误"))

    def test_a_wrong_shaped_reply_is_not_a_crash(self):
        with _Transport(_Response({"unexpected": True})):
            with self.assertRaises(se.TranslateError):
                se.OpenAIEngine("https://api.deepseek.com").chat([], "k", "m")


class FreeEngine(unittest.TestCase):
    def engine(self):
        return se.GoogleFreeEngine()

    def test_the_batch_body_has_the_shape_google_expects(self):
        with _Transport(_Response([[["你好"]]])) as t:
            self.engine().translate("Hello")
        self.assertEqual(t.body(), [[["Hello"], "auto", "zh-CN"], "wt_lib"])
        self.assertEqual(t.url(), se.GOOGLE_BATCH_URL)

    def test_the_batch_carries_the_public_web_key(self):
        with _Transport(_Response([[["你好"]]])) as t:
            self.engine().translate("Hello")
        # urllib capitalises header names as it stores them, and get_header
        # does not repeat that for the caller.
        self.assertEqual(t.requests[0].get_header("X-goog-api-key"),
                         se.GOOGLE_WEB_KEY)

    def test_the_translation_is_dug_out_of_the_nested_reply(self):
        with _Transport(_Response([[["你好"]]])):
            self.assertEqual(self.engine().translate("Hello"), "你好")

    def test_the_system_prompt_is_ignored_and_the_user_text_is_translated(self):
        # The free endpoint has no notion of a system prompt. Direction comes
        # from the target language in the body, so dropping the role is safe.
        with _Transport(_Response([[["你好"]]])) as t:
            got = self.engine().chat(
                [{"role": "system", "content": "你是译者"},
                 {"role": "user", "content": "Hello"}], "", "m")
        self.assertEqual(got, "你好")
        self.assertEqual(t.body()[0][0], ["Hello"])

    def test_a_throttled_endpoint_falls_through_to_the_next(self):
        with _Transport(_http_error(429, "quota"),
                        _Response({"translation": "你好"})) as t:
            self.assertEqual(self.engine().translate("Hello"), "你好")
        self.assertIn("plausibility.cloud", t.url(1))

    def test_lingva_is_read_from_its_translation_field(self):
        with _Transport(_http_error(500), _Response({"translation": "你好"})):
            self.assertEqual(self.engine().translate("Hello"), "你好")

    def test_the_last_resort_joins_its_sentence_segments(self):
        segments = [[["你好", "Hello", None, None], ["世界", " world", None, None]],
                    None, "en"]
        with _Transport(_http_error(500), _http_error(500), _Response(segments)) as t:
            self.assertEqual(self.engine().translate("Hello world"), "你好世界")
        self.assertIn("translate.googleapis.com", t.url(2))

    def test_the_location_survives_when_nothing_answers(self):
        with _Transport(_http_error(429), _http_error(429), _http_error(429)):
            with self.assertRaises(se.TranslateError) as caught:
                self.engine().translate("Hello")
        message = str(caught.exception)
        for name in ("translate-pa", "lingva", "gtx"):
            self.assertIn(name, message, message)

    def test_a_throttled_call_says_so_rather_than_showing_the_status_code(self):
        # "HTTP 429" on its own reads like a bug in this app.
        self.assertIn("限流", se._free_reason(se.TranslateError("HTTP 429: quota")))
        self.assertEqual(se._free_reason(se.TranslateError("HTTP 500: oops")),
                         "HTTP 500: oops")

    def test_no_network_anywhere_stops_the_chain_at_the_first_attempt(self):
        # All three endpoints live on the internet, so an unreachable host is
        # not three failures to report - it is one, and retrying is pointless.
        with _Transport(urllib.error.URLError("offline")) as t:
            with self.assertRaises(se.TranslateError) as caught:
                self.engine().translate("Hello")
        self.assertTrue(str(caught.exception).startswith("网络错误"))
        self.assertEqual(len(t.requests), 1)

    def test_a_blackholed_dns_is_a_timeout_rather_than_a_hang(self):
        # The reported bug: no VPN, so getaddrinfo blocks for far longer than
        # urlopen's own timeout covers, and the click looks like it did nothing.
        # The wall clock is what has to cut it off.
        with _Transport(lambda: time.sleep(5)) as t:
            started = time.time()
            with self.assertRaises(se.TranslateError) as caught:
                self.engine().translate("Hello", timeout=0.2)
            elapsed = time.time() - started
        self.assertTrue(str(caught.exception).startswith("网络错误"))
        self.assertLess(elapsed, 1, "等到了 %s 秒，墙钟超时没生效" % elapsed)
        self.assertEqual(len(t.requests), 1)

    def test_an_empty_reply_moves_on_instead_of_returning_blank(self):
        with _Transport(_Response([[[""]]]), _Response({"translation": "你好"})):
            self.assertEqual(self.engine().translate("Hello"), "你好")

    def test_the_engine_declares_what_it_cannot_do(self):
        free = self.engine()
        self.assertFalse(free.can_json)
        self.assertFalse(free.can_system)
        self.assertFalse(free.needs_key)

    def test_a_paid_engine_declares_the_opposite(self):
        paid = se.OpenAIEngine("https://api.deepseek.com")
        self.assertTrue(paid.can_json)
        self.assertTrue(paid.can_system)
        self.assertTrue(paid.needs_key)

    def test_tags_are_refused_without_sending_anything(self):
        # The whole point of can_json: the tag dictionary must never reach a
        # machine translator, and the refusal has to cost no request.
        import slg_translate as st
        with _Transport() as t:  # any request would raise AssertionError
            with self.assertRaises(se.TranslateError):
                st.translate_tags(["netorare"], "", engine=self.engine())
        self.assertEqual(t.requests, [])


class Config(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.was = os.environ.pop(se.ENV_KEY, None)

    def tearDown(self):
        os.environ.pop(se.ENV_KEY, None)
        if self.was is not None:
            os.environ[se.ENV_KEY] = self.was
        self.conn.close()

    def test_an_empty_install_defaults_to_the_free_engine(self):
        # Nothing configured at all still translates: that is what makes the
        # feature usable by someone who will not sign up for an API key.
        config = se.resolve_config(self.conn)
        self.assertEqual(config.engine, se.ENGINE_FREE)
        self.assertTrue(config.is_free)
        self.assertFalse(config.can_translate_tags)
        self.assertIsInstance(config.build(), se.GoogleFreeEngine)

    def test_an_old_install_is_left_alone(self):
        # The pre-engine versions stored a key and a model and nothing else.
        # Missing provider has to mean DeepSeek or every one of those installs
        # silently repoints at a different service.
        slg_db.set_pref(self.conn, se.PREF_KEY, "sk-old")
        slg_db.set_pref(self.conn, se.PREF_MODEL, "deepseek-chat")
        config = se.resolve_config(self.conn)
        self.assertEqual(config.engine, se.ENGINE_OPENAI)
        self.assertEqual(config.provider, se.DEFAULT_PROVIDER)
        self.assertEqual(config.base_url, se.DEFAULT_BASE_URL)
        self.assertEqual(config.model, "deepseek-chat")
        self.assertTrue(config.can_translate_tags)

    def test_a_chosen_provider_supplies_its_own_base_url_and_model(self):
        slg_db.set_pref(self.conn, se.PREF_PROVIDER, "moonshot")
        slg_db.set_pref(self.conn, se.PREF_KEY, "sk-x")
        config = se.resolve_config(self.conn)
        self.assertEqual(config.base_url, "https://api.moonshot.cn/v1")
        self.assertEqual(config.model, "moonshot-v1-8k")
        self.assertIn("Kimi", config.describe())

    def test_a_hand_typed_base_url_beats_the_preset(self):
        slg_db.set_pref(self.conn, se.PREF_PROVIDER, "custom")
        slg_db.set_pref(self.conn, se.PREF_BASE_URL, "http://127.0.0.1:8000/v1")
        slg_db.set_pref(self.conn, se.PREF_KEY, "sk-x")
        config = se.resolve_config(self.conn)
        self.assertEqual(config.base_url, "http://127.0.0.1:8000/v1")
        self.assertEqual(config.build().base_url, "http://127.0.0.1:8000/v1")

    def test_an_unknown_provider_does_not_break_the_dialog(self):
        # A provider id from a future version, or a hand-edited database.
        slg_db.set_pref(self.conn, se.PREF_PROVIDER, "gone")
        self.assertEqual(se.resolve_config(self.conn).provider, se.DEFAULT_PROVIDER)

    def test_the_free_engine_is_honoured_even_with_a_key_lying_around(self):
        # Someone who switched to free on purpose must not have the key they
        # left in the field quietly start costing them money.
        slg_db.set_pref(self.conn, se.PREF_KEY, "sk-x")
        slg_db.set_pref(self.conn, se.PREF_ENGINE, se.ENGINE_FREE)
        config = se.resolve_config(self.conn)
        self.assertTrue(config.is_free)
        self.assertEqual(config.describe(), "免费机翻（Google 公开接口）")

    def test_an_explicit_key_is_used_and_beats_the_settings(self):
        slg_db.set_pref(self.conn, se.PREF_KEY, "sk-stored")
        config = se.resolve_config(self.conn, explicit_key="sk-flag")
        self.assertEqual(config.api_key, "sk-flag")

    def test_the_key_alone_decides_when_the_engine_was_never_chosen(self):
        # resolve_config must not write anything back: opening the settings
        # dialog and closing it is not a decision.
        slg_db.set_pref(self.conn, se.PREF_KEY, "sk-x")
        before = slg_db.get_pref(self.conn, se.PREF_ENGINE)
        se.resolve_config(self.conn)
        self.assertEqual(slg_db.get_pref(self.conn, se.PREF_ENGINE), before)
        self.assertIsNone(slg_db.get_pref(self.conn, se.PREF_PROVIDER))

    def test_a_free_engine_needs_no_key_to_probe(self):
        import slg_translate as st
        with _Transport(_Response([[["正常"]]])):
            self.assertEqual(st.probe("", engine=se.GoogleFreeEngine()), "正常")

    def test_the_paid_engine_still_insists_on_a_key(self):
        import slg_translate as st
        with self.assertRaises(st.TranslateError):
            st.probe("", engine=se.OpenAIEngine(se.DEFAULT_BASE_URL))


class Helpers(unittest.TestCase):
    def test_first_string_walks_the_nesting(self):
        # Lists only: the one reply that is an object keeps its translation
        # under a named field, and the caller digs that out itself.
        self.assertEqual(se._first_string([[["你好"]]]), "你好")
        self.assertEqual(se._first_string([["", "先跳过"], "你好"]), "先跳过")
        self.assertIsNone(se._first_string([[], "", None]))
        self.assertIsNone(se._first_string(None))
        self.assertIsNone(se._first_string({"translation": "你好"}))

    def test_error_detail_survives_a_body_that_is_not_json(self):
        exc = _http_error(500, "<html>gateway</html>")
        self.assertIn("gateway", se._error_detail(exc))

    def test_error_detail_pulls_out_the_provider_message(self):
        exc = _http_error(400, '{"error":{"message":"model not found"}}')
        self.assertEqual(se._error_detail(exc), "model not found")


class WallClockDeadline(unittest.TestCase):
    """The outside-the-call deadline that urlopen(timeout=) cannot provide.

    DNS resolution is the case that matters: getaddrinfo ignores the socket
    timeout entirely, so on a machine with a blackholed resolver the only thing
    that ends the wait is a budget enforced by the caller.
    """

    def test_a_call_that_finishes_in_time_returns_its_value(self):
        self.assertEqual(se._call_with_deadline(lambda: "ok", 5), "ok")

    def test_a_slow_call_raises_a_network_error_once_the_budget_is_gone(self):
        started = time.time()
        with self.assertRaises(se.TranslateError) as caught:
            se._call_with_deadline(lambda: time.sleep(5), 0.2)
        self.assertTrue(str(caught.exception).startswith("网络错误"))
        self.assertLess(time.time() - started, 1)

    def test_a_timeout_is_a_translate_error_so_the_chain_treats_it_as_unreachable(self):
        # It has to inherit, not just look like one: _free_failure branches on
        # the class and the message prefix to decide whether to keep trying.
        self.assertTrue(issubclass(se._Timeout, se.TranslateError))

    def test_the_abandoned_thread_is_a_daemon(self):
        # Non-daemon workers get joined at interpreter exit, so a hung resolver
        # would keep the process alive after the window closes - worse than the
        # leak it would be avoiding. This is what keeps a ThreadPoolExecutor out
        # of that spot.
        before = set(threading.enumerate())
        with self.assertRaises(se.TranslateError):
            se._call_with_deadline(lambda: time.sleep(5), 0.1)
        fresh = set(threading.enumerate()) - before
        self.assertTrue(fresh, "被放弃的调用没有留下可检查的线程")
        for thread in fresh:
            self.assertTrue(thread.daemon, "线程 %s 不是 daemon" % thread.name)

    def test_an_exception_from_the_call_is_re_raised_unchanged(self):
        def boom():
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            se._call_with_deadline(boom, 5)


if __name__ == "__main__":
    unittest.main()
