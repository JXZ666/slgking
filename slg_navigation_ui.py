"""Keyboard navigation adapter. All methods are called on the Tk thread.

Integration: call query_started at each query entry, query_completed after rows
are installed (skip automatic detail rendering when it returns True), invalidate
before theme rebuild, and mouse_select at the start of ordinary select().
"""
import tkinter as tk
from slg_keyboard_navigation import NavigationIntent, visible_scroll_fraction


class NavigationUI:
    def _navigation_invalidate(self):
        self._navigation_epoch = getattr(self, '_navigation_epoch', 0) + 1
        for name in ('_navigation_detail_job', '_navigation_scroll_job'):
            job = getattr(self, name, None)
            if job is not None:
                try: self.after_cancel(job)
                except (tk.TclError, ValueError): pass
            setattr(self, name, None)
        self._navigation_intent = None
        self._navigation_enter = False
        self._navigation_target_context = None
        self._navigation_target_gid = None

    def _navigation_mouse_select(self, game=None):
        self._navigation_invalidate()
        self._navigation_detail_gid = None
        if game is not None and self._navigation_collections():
            self._navigation_paint_collection_selection(game['id'])

    def _navigation_query_started(self):
        if not getattr(self, '_navigation_request', False):
            self._navigation_invalidate()
        self._navigation_read_pending = True

    def _navigation_context(self):
        return (self._library_identity(), getattr(self, '_workspace_mode', None),
                self.origin, self._navigation_page(), getattr(self, 'catalogue_id', None),
                getattr(self, 'collection_id', None),
                getattr(self, '_collections_spec', {}).get('collection_id'),
                id(getattr(self, 'list', None)), getattr(self, '_sys_theme', None),
                getattr(self, '_navigation_epoch', 0))

    def _navigation_collections(self):
        return getattr(self, '_workspace_mode', None) == 'collections'

    def _navigation_rows(self):
        return (getattr(self, '_navigation_collection_games', [])
                if self._navigation_collections() else self._page_slice())

    def _navigation_page(self):
        if self._navigation_collections():
            return (self._collections_overview_page if self._collections_spec.get('collection_id') is None
                    else self._collections_page)
        return self.page

    def _navigation_pending(self):
        return bool(getattr(self, '_navigation_read_pending', False))

    def _card_key_owns_keystroke(self):
        try:
            focused = self.focus_get()
            if self.grab_current() is not None: return True
            if focused is not None and focused.winfo_toplevel() is not self: return True
            if isinstance(focused, (tk.Entry, tk.Text, tk.Spinbox, tk.Listbox, tk.Scale)): return True
            if focused is not None and focused.winfo_class() in ('TEntry', 'TCombobox', 'TSpinbox', 'Menu'): return True
            menu = getattr(self, '_slg_action_menu', None)
            popup = getattr(menu, 'popup', None)
            if popup is not None and popup.winfo_exists(): return True
            if self.tk.call('info', 'exists', '::tk::Priv(postedMb)'):
                if self.tk.call('set', '::tk::Priv(postedMb)'): return True
        except (KeyError, tk.TclError):
            # A disappearing dialog/focus target must not leak keys to old rows.
            return True
        return (getattr(self, '_panel_mode', None) in ('shop', 'comments', 'profile')
                or getattr(self, '_theme_rebuilding', False))

    def _navigation_state(self):
        state = getattr(self, '_navigation_intent', None)
        if state is None:
            if self._navigation_collections():
                size = 12 if self._collections_spec.get('collection_id') is None else 8
                total = getattr(self, '_navigation_collection_total', len(self._navigation_rows()))
            else:
                import slg_gui
                size = slg_gui.PAGE_SIZE
                total = (getattr(self, '_library_total', 0) if self.origin == 'local'
                         else getattr(self, '_catalogue_total', len(self.rows)))
            self._navigation_intent = state = NavigationIntent(total, size)
        return state

    def _navigation_turn_to(self, page):
        before = self._navigation_page()
        if before == page: return
        self._navigation_request = True
        try:
            if self._navigation_collections():
                if self._collections_spec.get('collection_id') is None:
                    self._collections_overview_page = page
                else: self._collections_page = page
                self.refresh_collections_workspace()
            else: self._goto_page(page)
        finally: self._navigation_request = False

    def _navigation_apply_intent(self):
        state = getattr(self, '_navigation_intent', None)
        if state is None or state.target is None: return False
        if self._navigation_pending(): return True
        if state.page != self._navigation_page():
            self._navigation_turn_to(state.page)
            if self._navigation_pending(): return True
        if self._navigation_collections() and self._collections_spec.get('collection_id') is None:
            self._navigation_intent = None
            return True
        rows = self._navigation_rows()
        index = state.target % state.size
        if rows:
            self._navigation_highlight(rows[min(index, len(rows)-1)])
        self._navigation_intent = None
        return True

    def _navigation_query_completed(self, error=False):
        self._navigation_read_pending = False
        if error:
            self._navigation_invalidate()
            return True
        if self._navigation_collections():
            self._navigation_paint_collection_selection(self.selected['id'] if self.selected is not None else None)
        state = getattr(self, '_navigation_intent', None)
        if state is not None:
            state.total = (getattr(self, '_navigation_collection_total', 0) if self._navigation_collections()
                           else getattr(self, '_library_total', 0) if self.origin == 'local'
                           else getattr(self, '_catalogue_total', len(self.rows)))
            if state.total <= 0:
                self._navigation_intent = None
                return True
            if state.target is not None: state.target = min(state.target, state.total - 1)
        return self._navigation_apply_intent()

    def _move_selection(self, delta):
        state = self._navigation_state()
        rows = self._navigation_rows()
        gid = self.selected['id'] if self.selected is not None else None
        index = next((i for i, row in enumerate(rows) if row['id'] == gid), None)
        state.move(delta, self._navigation_page(), index)
        self._navigation_apply_intent()
        return 'break'

    def _navigation_turn_page(self, delta):
        self._navigation_state().turn(delta, self._navigation_page())
        self._navigation_apply_intent()
        return 'break'

    def _navigation_highlight(self, game):
        old = self.selected
        self.selected = dict(game)
        if old is not None: self._paint_card(old['id'], False)
        self._paint_card(game['id'], True)
        if self._navigation_collections():
            self._navigation_paint_collection_selection(game['id'])
        self._scroll_card_into_view(game['id'])
        job = getattr(self, '_navigation_detail_job', None)
        if job is not None:
            try: self.after_cancel(job)
            except (tk.TclError, ValueError): pass
        context, gid = self._navigation_context(), game['id']
        self._navigation_target_context, self._navigation_target_gid = context, gid
        def commit():
            self._navigation_detail_job = None
            if (context != self._navigation_context() or self._navigation_pending()
                    or self.selected is None or self.selected['id'] != gid
                    or self._card_key_owns_keystroke()): return
            self._navigation_commit_detail()
        self._navigation_detail_job = self.after(0 if getattr(self, '_navigation_enter', False) else 100, commit)
        self._navigation_enter = False

    def _navigation_paint_collection_selection(self, gid):
        import slg_gui as gui
        for row in getattr(self, '_collections_rows', ()):
            sig = getattr(row, '_signature', None)
            selected = bool(sig and sig[0] == 'member' and sig[1] == gid)
            if getattr(row, '_navigation_selected', None) == selected: continue
            row._navigation_selected = selected
            row.configure(border_width=2 if selected else 0,
                          border_color=(gui._LIGHT['ACCENT'], gui._DARK['ACCENT']))

    def _navigation_cards_rendered(self):
        if (self.selected is not None
                and getattr(self, '_navigation_target_context', None) == self._navigation_context()
                and getattr(self, '_navigation_target_gid', None) == self.selected['id']):
            self._scroll_card_into_view(self.selected['id'])

    def _navigation_commit_detail(self):
        self._panel_mode = 'game'
        self._set_shop_wide_layout(False)
        self._render_detail_if_stale()
        self._navigation_detail_gid = self.selected['id']

    def _activate_selected_card(self):
        if self._navigation_pending():
            self._navigation_enter = True
            if getattr(self, '_navigation_intent', None) is None:
                self._navigation_state().move(1, self._navigation_page())
            return 'break'
        rows = self._navigation_rows()
        gid = self.selected['id'] if self.selected is not None else None
        game = next((row for row in rows if row['id'] == gid), rows[0] if rows else None)
        if game is not None:
            self._navigation_highlight(game)
            job = self._navigation_detail_job
            if job is not None: self.after_cancel(job)
            self._navigation_detail_job = None
            self._navigation_commit_detail()
        return 'break'

    def _scroll_card_into_view(self, gid):
        job = getattr(self, '_navigation_scroll_job', None)
        if job is not None:
            try: self.after_cancel(job)
            except (tk.TclError, ValueError): pass
        context = self._navigation_context()
        def apply():
            self._navigation_scroll_job = None
            if (context != self._navigation_context() or self.selected is None
                    or self.selected['id'] != gid): return
            if self._navigation_collections():
                canvas = getattr(self._collections_list, '_parent_canvas', None)
                frame = next((row for row in self._collections_rows
                              if getattr(row, '_signature', None) and row._signature[:2] == ('member', gid)), None)
            else:
                canvas = getattr(self.list, '_parent_canvas', None)
                frame = next((slot['frame'] for row_gid, slot in zip(self._pool_gid, self._card_pool) if row_gid == gid), None)
            if canvas is None or frame is None: return
            try:
                region = canvas.bbox('all')
                if not region: return
                top = frame.winfo_y()
                fraction = visible_scroll_fraction(top, top + frame.winfo_height(),
                    canvas.canvasy(0), canvas.winfo_height(), region[1], region[3])
                if fraction is not None: canvas.yview_moveto(fraction)
            except tk.TclError: pass
        self._navigation_scroll_job = self.after_idle(apply)

    def _on_card_key(self, event):
        if self._card_key_owns_keystroke(): return None
        key = event.keysym
        overview = self._navigation_collections() and self._collections_spec.get('collection_id') is None
        if key in ('Left', 'Prior'): return self._navigation_turn_page(-1)
        if key in ('Right', 'Next'): return self._navigation_turn_page(1)
        if overview: return None
        if key == 'Up': return self._move_selection(-1)
        if key == 'Down': return self._move_selection(1)
        if key in ('Return', 'KP_Enter'): return self._activate_selected_card()
        return None
