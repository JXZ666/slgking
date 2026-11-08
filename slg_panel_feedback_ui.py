"""User-entry glue for finite feedback on the five right-side pages."""
from slg_panel_feedback import PanelFeedback

PANEL_TITLES = {'profile': '个人中心', 'shop': '积分商城', 'settings': '设置',
                'leaderboard': '排行榜', 'tools': '更多工具'}
NAV_LABELS = dict(PANEL_TITLES, tools='更多工具…')
PREF_HINT = 'ui.right_panel_hint_seen'


class PanelFeedbackUI:
    def _init_panel_feedback(self):
        import slg_db
        import slg_gui as g
        self._cancel_panel_entry_feedback()
        previous = getattr(self, '_panel_feedback', None)
        if previous is not None:
            previous.close()
        self._panel_feedback_headers = {}
        self._panel_feedback = PanelFeedback(self, self._micro_motion,
            seen=lambda _key: slg_db.get_pref(self.conn, PREF_HINT, '0') == '1',
            mark_seen=lambda _key: slg_db.set_pref(self.conn, PREF_HINT, '1'),
            palette=lambda: {key: getattr(g, key) for key in ('BG', 'CARD', 'TEXT', 'MUTED', 'ACCENT')},
            font_factory=g.ui_font)

    def _make_panel_feedback_header(self, key, parent, title=None, **kwargs):
        header = self._panel_feedback.attach(key, parent, title or PANEL_TITLES[key], **kwargs)
        self._panel_feedback_headers[key] = header
        return header

    def _open_panel_entry(self, key, command, *, refresh=False):
        """Only explicit user navigation calls this; data refreshes use the core methods."""
        header = getattr(self, '_panel_feedback_headers', {}).get(key)
        feedback = getattr(self, '_panel_feedback', None)
        pending = getattr(self, '_panel_feedback_pending', None)
        current = (getattr(self, '_panel_mode', None) == key and header is not None
                   and header.winfo_exists() and feedback is not None
                   and (feedback.active_key == key or (pending is not None
                        and pending[0] == key and pending[1] is header)))
        # Post-action return buttons may retain their existing reconciliation.
        result = None if current and not refresh else command()
        if getattr(self, '_panel_mode', None) == key:
            header = getattr(self, '_panel_feedback_headers', {}).get(key)
            if header is not None and header.winfo_exists():
                self._panel_feedback_pending = (key, header,
                    getattr(self, '_identity_generation', 0), self._panel_feedback)
                if getattr(self, '_panel_feedback_entry_job', None) is None:
                    self._panel_feedback_entry_job = self.after(0, self._run_panel_entry_feedback)
        return result

    def _run_panel_entry_feedback(self):
        self._panel_feedback_entry_job = None
        pending = getattr(self, '_panel_feedback_pending', None)
        self._panel_feedback_pending = None
        if pending is None:
            return
        key, header, generation, feedback = pending
        if (generation != getattr(self, '_identity_generation', 0)
                or feedback is not getattr(self, '_panel_feedback', None)
                or getattr(self, '_panel_mode', None) != key
                or getattr(self, '_panel_feedback_headers', {}).get(key) is not header
                or not self.winfo_viewable() or self.focus_displayof() is None
                or not feedback._live(header)):
            return
        self._animate_nav_selection(NAV_LABELS[key])
        feedback.activate(key, header, user_initiated=True)

    def _cancel_panel_entry_feedback(self, key=None):
        pending = getattr(self, '_panel_feedback_pending', None)
        if key is not None and (pending is None or pending[0] != key):
            return
        self._panel_feedback_pending = None
        job = getattr(self, '_panel_feedback_entry_job', None)
        self._panel_feedback_entry_job = None
        if job is not None:
            self.after_cancel(job)

    def _deactivate_panel_feedback(self, key=None):
        self._cancel_panel_entry_feedback(key)
        feedback = getattr(self, '_panel_feedback', None)
        if feedback is not None and (key is None or feedback.active_key == key):
            feedback.deactivate()
        selected = getattr(self, '_nav_selected_widget', None)
        if selected is not None and selected.winfo_exists():
            if selected.cget('text') in NAV_LABELS.values() and (key is None or selected.cget('text') == NAV_LABELS[key]):
                import slg_gui as g
                selected.configure(border_color=g.SIDEBAR)
                self._nav_selected_widget = None

    def _close_panel_feedback(self):
        self._cancel_panel_entry_feedback()
        feedback = getattr(self, '_panel_feedback', None)
        if feedback is not None:
            feedback.close()
        self._panel_feedback = None
        self._panel_feedback_headers = {}
