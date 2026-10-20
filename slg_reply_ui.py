"""Disposable, on-demand flat discussion views for 0.28.0."""
from concurrent.futures import ThreadPoolExecutor
import uuid
import customtkinter as ctk
from slg_image_resources import ResourceButton, ResourceLabel
import slg_comment_replies as api
import slg_account


class CommunityTasks:
    """Bounded transport work; workers only enqueue main-thread callbacks."""
    def _community_request(self, key, work, done, window=None):
        if not hasattr(self, '_community_jobs'):
            self._community_jobs = {}
        if key in self._community_jobs:
            return False
        if len(self._community_jobs) >= 8:
            done(None, '资料请求较多，请稍候再试')
            return False
        executor = getattr(self, '_community_executor', None)
        if executor is None:
            executor = self._community_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='community')
        generation = getattr(self, '_community_generation', 0)
        identity = self._library_identity()
        try: session_snapshot = slg_account.session()
        except slg_account.AccountError: session_snapshot = None
        marker = object()
        self._community_jobs[key] = (marker, None)
        def complete(value, error):
            if self._community_jobs.get(key, (None,))[0] is not marker: return
            self._community_jobs.pop(key, None)
            if generation != getattr(self, '_community_generation', 0) or identity != self._library_identity(): return
            if window is not None:
                try:
                    if not window.winfo_exists(): return
                except Exception: return
            done(value, error)
        def worker():
            try:
                if generation != getattr(self, '_community_generation', 0):
                    value, error = None, '请求已取消'
                else:
                    with slg_account.use_session_snapshot(session_snapshot):
                        value, error = work(), None
            except Exception as exc: value, error = None, str(exc)
            self.queue.put(('library_callback', (complete, value, error)))
        future = executor.submit(worker)
        self._community_jobs[key] = (marker, future)
        self._wake_queue()
        return True

    def _reset_community_ui(self):
        self._community_generation = getattr(self, '_community_generation', 0) + 1
        for _, future in getattr(self, '_community_jobs', {}).values():
            if future is not None: future.cancel()
        self._community_jobs = {}
        for win in getattr(self, '_community_windows', []):
            try: win.destroy()
            except Exception: pass
        self._community_windows = []
        self._notification_window = None
        self._notification_snapshot = None
        self._notification_identity = None
        self._notification_loading = False
        self._notification_error = None

    def _dispose_community_ui(self):
        self._reset_community_ui()
        executor = getattr(self, '_community_executor', None)
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
            self._community_executor = None

    def _community_track_window(self, win):
        windows = getattr(self, '_community_windows', [])
        self._community_windows = [value for value in windows if value.winfo_exists()] + [win]
        return win


class ReplyUI(CommunityTasks):
    def _can_write_replies(self):
        if not self._has_usable_cloud_session(): return False
        if getattr(self, '_is_admin_mode', lambda: False)(): return False
        identity = self._library_identity()
        return not (isinstance(identity, dict) and
                    (identity.get('admin') or identity.get('account_id') == 'developer'))

    def _build_comment_replies(self, parent, root_comment):
        import slg_gui as g
        root_id = root_comment.get('cloud_id') or root_comment.get('id')
        if not root_id: return None
        count = root_comment.get('reply_count', 0)
        count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
        frame = ctk.CTkFrame(parent, fg_color='transparent')
        frame.pack(fill='x', padx=8, pady=(0, 8))
        state = {'expanded': False, 'body': None}
        def toggle():
            if state['expanded']:
                state['body'].destroy()
                state['expanded'] = False
                button.configure(text=f'展开回复 · {count} 条')
            else:
                state['expanded'] = True
                button.configure(text='收起回复')
                state['body'] = self._reply_thread(frame, root_id)
        button = ResourceButton(frame, text=f'展开回复 · {count} 条', height=34,
            fg_color=g.CHIP, text_color=g.TEXT, font=g.ui_font(size=12), command=toggle)
        button.pack(anchor='w')
        frame._reply_toggle = toggle
        return frame

    def _reply_thread(self, parent, root_id, focus_id=None, loaded=None):
        import slg_gui as g
        frame = ctk.CTkFrame(parent, fg_color='transparent')
        frame.pack(fill='both', expand=True, pady=6)
        status = ResourceLabel(frame, text='正在读取回复…', text_color=g.MUTED, font=g.ui_font(size=12))
        status.pack(fill='x')
        rows = ctk.CTkScrollableFrame(frame, fg_color=g.CARD, height=230)
        rows.pack(fill='both', expand=True)
        pager = ctk.CTkFrame(frame, fg_color='transparent'); pager.pack(fill='x', pady=4)
        compose = ctk.CTkFrame(frame, fg_color='transparent'); compose.pack(fill='x')
        target = ResourceLabel(compose, text='回复原评论', text_color=g.MUTED, font=g.ui_font(size=12)); target.pack(anchor='w')
        entry = ctk.CTkTextbox(compose, height=85, font=g.ui_font(size=13), text_color=g.TEXT)
        entry.pack(fill='x', pady=4)
        state = {'page': 1, 'target': None, 'sending': False, 'loaded': False, 'operation': None, 'content': None, 'seq': 0}
        logged = bool(self._has_usable_cloud_session())
        can_write = self._can_write_replies()
        def set_target(row=None):
            state['target'] = row.get('id') if row else None
            target.configure(text='回复 ' + str(row.get('nickname') or '用户') if row else '回复原评论')
        def load(page=1, focus=None):
            state['seq'] += 1
            sequence = state['seq']
            status.configure(text='正在读取回复…')
            def work():
                result = api.fetch_replies(root_id, page, focus)
                mine, own_error = [], None
                if can_write:
                    try: mine = api.fetch_my_replies(root_id).get('replies', [])
                    except Exception as exc: own_error = str(exc)
                return result, mine, own_error
            def done(value, error):
                if sequence != state['seq']: return
                for widget in rows.winfo_children(): widget.destroy()
                for widget in pager.winfo_children(): widget.destroy()
                if error:
                    status.configure(text='读取失败，未显示旧内容：' + error)
                    ResourceButton(rows, text='重试', command=lambda: load(page, focus), text_color=g.TEXT, fg_color=g.CHIP).pack(pady=8)
                    return
                data, mine, own_error = value
                state['page'] = int(data.get('page') or 1)
                own_ids = {str(row.get('id')) for row in mine}
                own_public_id = None
                cache = getattr(self, '_cloud_me', None)
                identity = self._library_identity()
                if (logged and isinstance(cache, dict) and isinstance(identity, dict)
                        and cache.get('account_id') == identity.get('account_id')):
                    own_public_id = cache.get('public_profile_id')
                public = data.get('replies', [])
                values = [dict(row, own=str(row.get('id')) in own_ids or row.get('own') is True or bool(own_public_id and row.get('author_public_id') == own_public_id)) for row in public]
                if state['page'] == 1:
                    seen = {str(row.get('id')) for row in values}
                    values += [dict(row, own=True) for row in mine if str(row.get('id')) not in seen and row.get('status') not in ('approved', 'public')]
                for raw in values:
                    row = api.safe_reply(raw)
                    box = ctk.CTkFrame(rows, fg_color=g.CARD); box.pack(fill='x', pady=4)
                    name = str(row.get('nickname') or '匿名用户')[:40]
                    line = name + (' → ' + str(row['reply_to_nickname'])[:40] if row.get('reply_to_nickname') else '')
                    ResourceButton(box, text=line, anchor='w', height=30, fg_color='transparent', text_color=g.TEXT,
                        command=lambda row=row: self._show_public_author_card({'nickname': row.get('nickname'), 'public_id': row.get('author_public_id')})).pack(fill='x')
                    ResourceLabel(box, text=str(row.get('content') or ''), wraplength=500, anchor='w', justify='left', text_color=g.TEXT, font=g.ui_font(size=13)).pack(fill='x', padx=8)
                    controls = ctk.CTkFrame(box, fg_color='transparent'); controls.pack(fill='x', padx=8, pady=3)
                    if row.get('own'):
                        labels = {'pending': '待审核', 'rejected': '未通过审核', 'approved': '已公开', 'public': '已公开'}
                        ResourceLabel(controls, text=labels.get(row.get('status'), '状态待确认'), text_color=g.MUTED, font=g.ui_font(size=11)).pack(side='left')
                    if can_write:
                        if row.get('status') in ('approved', 'public'):
                            ResourceButton(controls, text='回复', width=66, height=30, fg_color=g.CHIP, text_color=g.TEXT, command=lambda row=row: set_target(row)).pack(side='right', padx=3)
                        action = api.delete_reply if row.get('own') else api.report_reply
                        ResourceButton(controls, text='删除' if row.get('own') else '举报', width=66, height=30, fg_color=g.CHIP, text_color=g.TEXT,
                            command=lambda row=row, action=action: act(row, action)).pack(side='right', padx=3)
                if not values: ResourceLabel(rows, text='暂无公开回复', text_color=g.MUTED).pack(pady=15)
                status.configure(text=f'第 {state["page"]} 页 · 公开 {int(data.get("total_count") or 0)} 条' + ('；本人审核状态读取失败，可重试' if own_error else ''))
                for label, enabled, destination in [('上一页', data.get('has_previous'), state['page'] - 1), ('下一页', data.get('has_next'), state['page'] + 1)]:
                    ResourceButton(pager, text=label, width=90, height=34, state='normal' if enabled else 'disabled', fg_color=g.CHIP, text_color=g.TEXT, command=lambda p=destination: load(p)).pack(side='left', padx=4)
                if loaded and not state['loaded']:
                    if focus_id is None or any(str(row.get('id')) == str(focus_id) for row in public):
                        state['loaded'] = True; loaded()
                    else:
                        status.configure(text='目标回复已不可查看；没有标记消息已读')
            self._community_request(('replies', id(frame), sequence), work, done, frame)
        def act(row, action):
            if not self._can_write_replies():
                status.configure(text='请切换普通账号后操作'); return
            status.configure(text='正在提交…')
            def done(value, error):
                if error: status.configure(text='操作失败：' + error)
                elif action is api.delete_reply: load(state['page'])
                else: status.configure(text='举报已提交')
            self._community_request(('reply-action', row['id']), lambda: action(row['id']), done, frame)
        def send():
            if not self._can_write_replies():
                status.configure(text='请切换普通账号后发布追评'); return
            if state['sending']: return
            content = entry.get('1.0', 'end').strip()
            if not 2 <= len(content) <= 500:
                status.configure(text='追评需要 2 至 500 个字符'); return
            # Reuse operation id after an uncertain transport outcome, preventing duplicates.
            if state['content'] != (content, state['target']):
                state['content'] = (content, state['target']); state['operation'] = str(uuid.uuid4())
            state['sending'] = True; send_button.configure(state='disabled')
            status.configure(text='正在提交追评…')
            def done(value, error):
                state['sending'] = False; send_button.configure(state='normal')
                if error: status.configure(text='提交失败，正文已保留：' + error); return
                reply = value.get('reply', {})
                if reply.get('status') == 'rejected':
                    status.configure(text='未通过审核，正文已保留，请修改后重试'); return
                entry.delete('1.0', 'end'); set_target(); state['operation'] = None; state['content'] = None
                load(1)
            target_id, operation_id = state['target'], state['operation']
            self._community_request(('reply-submit', id(frame)), lambda: api.submit_reply(root_id, content, target_id, operation_id), done, frame)
        send_button = ResourceButton(compose, text='发布追评' if can_write else '开发者模式仅支持阅读' if logged else '登录后可发布追评', height=38, state='normal' if can_write else 'disabled', fg_color=g.ACCENT, hover_color=g.ACCENT_HOVER, text_color=g.ON_ACCENT, text_color_disabled=g.MUTED, command=send)
        send_button.pack(side='right', pady=4)
        ResourceButton(compose, text='回复原评论', height=38, fg_color=g.CHIP, text_color=g.TEXT, command=set_target).pack(side='left', pady=4)
        if not can_write: entry.configure(state='disabled')
        frame._reply_state = state
        frame._reply_entry = entry
        frame._reply_load = load
        load(focus=focus_id)
        return frame

    def _open_reply_context(self, reply_id, notification_id=None):
        import slg_gui as g
        win = self._community_track_window(self._new_dialog('评论讨论', '760x760'))
        status = ResourceLabel(win, text='正在读取讨论…', text_color=g.MUTED, font=g.ui_font(size=13)); status.pack(fill='x', padx=16, pady=12)
        def work(): return api.fetch_context(reply_id)
        def done(value, error):
            if error:
                status.configure(text='讨论不可用或读取失败；未显示旧内容：' + error); return
            root = value.get('root')
            if not isinstance(root, dict): status.configure(text='服务器未返回有效评论'); return
            status.configure(text=str(root.get('nickname') or root.get('author') or '用户') + ' · ' + str(value.get('game') or ''))
            ResourceLabel(win, text=str(root.get('content') or ''), text_color=g.TEXT, font=g.ui_font(size=13), wraplength=650, justify='left').pack(fill='x', padx=16, pady=8)
            def loaded():
                if notification_id is not None:
                    self._community_request(('notice-read', str(notification_id)), lambda: api.mark_notifications_read([notification_id]), lambda value, error: self._refresh_notifications() if not error else None, win)
            self._reply_thread(win, root.get('id') or root.get('cloud_id'), focus_id=reply_id, loaded=loaded)
        self._community_request(('context', id(win)), work, done, win)
        return win

