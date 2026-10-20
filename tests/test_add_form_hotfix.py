import unittest

from slg_add_form_state import AddFormState, FIELDS, RequestToken


class AddFormHotfixTests(unittest.TestCase):
    def test_empty_initial_state(self):
        state = AddFormState()
        self.assertEqual(state.values, dict.fromkeys(FIELDS, ""))
        self.assertEqual(set(state.sources.values()), {"empty"})
        self.assertFalse(state.pending)

    def test_edit_existing_values_are_manual(self):
        state = AddFormState({"title": "保存名", "engine": "Unity"})
        token = state.begin_request("new")
        state.apply(token, {"title": "探测名", "engine": "RenPy", "version": "2"})
        self.assertEqual(state.values["title"], "保存名")
        self.assertEqual(state.values["engine"], "Unity")
        self.assertEqual(state.values["version"], "2")

    def test_switch_clears_auto_immediately(self):
        state = AddFormState()
        state.apply(state.begin_request("A"), dict.fromkeys(FIELDS, "旧"))
        state.edit("developer", "手工")
        state.begin_request("B")
        self.assertTrue(state.pending)
        self.assertEqual(state.values, {"title": "", "developer": "手工", "engine": "", "version": ""})

    def test_new_information_replaces_all_auto_fields(self):
        state = AddFormState()
        state.apply(state.begin_request("A"), dict.fromkeys(FIELDS, "A"))
        state.apply(state.begin_request("B"), dict.fromkeys(FIELDS, "B"))
        self.assertEqual(set(state.values.values()), {"B"})

    def test_unknown_new_fields_do_not_keep_old_information(self):
        state = AddFormState()
        state.apply(state.begin_request("A"), dict.fromkeys(FIELDS, "old"))
        state.apply(state.begin_request("B"), {"title": "new", "engine": None})
        self.assertEqual(state.values, {"title": "new", "developer": "", "engine": "", "version": ""})

    def test_manual_edit_during_request_survives(self):
        state = AddFormState()
        token = state.begin_request("A")
        state.edit("title", "用户正在编辑")
        state.apply(token, {"title": "迟到", "engine": "Unity"})
        self.assertEqual(state.values["title"], "用户正在编辑")
        self.assertEqual(state.values["engine"], "Unity")

    def test_manual_clear_is_preserved_across_requests(self):
        state = AddFormState()
        state.edit("engine", "")
        state.apply(state.begin_request("A"), {"engine": "Unity"})
        state.apply(state.begin_request("B"), {"engine": "RenPy"})
        self.assertEqual(state.values["engine"], "")
        self.assertEqual(state.sources["engine"], "manual")

    def test_a_b_a_has_three_distinct_generations(self):
        state = AddFormState()
        first = state.begin_request("A")
        second = state.begin_request("B")
        third = state.begin_request("A")
        self.assertFalse(state.apply(first, {"title": "old-A"}))
        self.assertFalse(state.apply(second, {"title": "old-B"}))
        self.assertTrue(state.pending)
        self.assertTrue(state.apply(third, {"title": "new-A"}))
        self.assertEqual(state.values["title"], "new-A")

    def test_foreign_or_reconstructed_token_cannot_apply(self):
        state = AddFormState()
        token = state.begin_request("A")
        foreign = AddFormState().begin_request("A")
        clone = RequestToken(token.generation, token.path)
        for invalid in (foreign, clone, None):
            self.assertFalse(state.apply(invalid, {}))

    def test_request_can_apply_only_once(self):
        state = AddFormState()
        token = state.begin_request("A")
        self.assertTrue(state.apply(token, {"title": "first"}))
        self.assertFalse(state.apply(token, {"title": "duplicate"}))
        self.assertEqual(state.values["title"], "first")

    def test_fail_leaves_manual_but_not_previous_auto(self):
        state = AddFormState()
        state.apply(state.begin_request("A"), {"title": "A", "version": "1"})
        state.edit("title", "custom")
        token = state.begin_request("B")
        self.assertTrue(state.fail(token))
        self.assertFalse(state.pending)
        self.assertEqual(state.values["version"], "")
        self.assertEqual(state.values["title"], "custom")
        self.assertFalse(state.apply(token, {"version": "late"}))

    def test_stale_failure_does_not_end_active_request(self):
        state = AddFormState()
        old = state.begin_request("A")
        new = state.begin_request("B")
        self.assertFalse(state.fail(old))
        self.assertTrue(state.pending)
        self.assertTrue(state.fail(new))

    def test_force_adopts_last_info_and_clears_unknown(self):
        state = AddFormState({"title": "custom", "version": "custom"})
        token = state.begin_request("A")
        state.apply(token, {"title": "recognized"})
        self.assertTrue(state.force_apply(token))
        self.assertEqual(state.values["title"], "recognized")
        self.assertEqual(state.values["version"], "")
        self.assertEqual(state.sources["title"], "auto")

    def test_force_cannot_use_stale_info(self):
        state = AddFormState()
        old = state.begin_request("A")
        state.apply(old, {"title": "old"})
        new = state.begin_request("B")
        self.assertFalse(state.force_apply(old, {"title": "wrong"}))
        self.assertFalse(state.force_apply(new))
        self.assertTrue(state.pending)

    def test_force_explicit_info_can_complete_request(self):
        state = AddFormState({"title": "custom"})
        token = state.begin_request("A")
        self.assertTrue(state.force_apply(token, {"title": "A"}))
        self.assertFalse(state.pending)
        self.assertEqual(state.values["title"], "A")

    def test_close_or_identity_change_invalidates_all_results(self):
        state = AddFormState()
        token = state.begin_request("A")
        state.invalidate()
        self.assertFalse(state.pending)
        self.assertIsNone(state.current_token)
        self.assertFalse(state.apply(token, {"title": "late"}))
        self.assertFalse(state.force_apply(token, {"title": "late"}))
        self.assertFalse(state.force_apply(None, {}))

    def test_properties_return_copies(self):
        state = AddFormState()
        state.apply(state.begin_request("A"), {"title": "A"})
        state.values["title"] = "bad"
        state.sources["title"] = "bad"
        state.last_info["title"] = "bad"
        self.assertEqual(state.values["title"], "A")
        self.assertEqual(state.sources["title"], "auto")
        self.assertEqual(state.last_info["title"], "A")

    def test_normalizes_only_recognized_values_and_preserves_manual_text(self):
        state = AddFormState()
        state.edit("developer", "  intentional  ")
        state.apply(state.begin_request("A"), {"title": "  A  ", "engine": None, "version": 2})
        self.assertEqual(state.values["title"], "A")
        self.assertEqual(state.values["developer"], "  intentional  ")
        self.assertEqual(state.values["engine"], "")
        self.assertEqual(state.values["version"], "2")

    def test_invalid_field_or_result_does_not_mutate_state(self):
        state = AddFormState()
        token = state.begin_request("A")
        with self.assertRaises(KeyError):
            state.edit("not-a-field", "bad")
        with self.assertRaises(TypeError):
            state.apply(token, None)
        self.assertTrue(state.pending)
        self.assertEqual(set(state.values.values()), {""})

    def test_long_unicode_path_is_opaque_and_preserved(self):
        state = AddFormState()
        path = "D:/游戏/" + "非常长的文件夹名称" * 100
        token = state.begin_request(path)
        self.assertEqual(token.path, path)
        self.assertEqual(state.current_path, path)


if __name__ == "__main__":
    unittest.main()
