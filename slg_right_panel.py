import slg_page_scroll
"""Right-pane pages with a viewport-sized host and page-owned scrolling.

GUI integration calls the methods on RightPanelUI explicitly. This module does
not patch App or dependency classes and never creates a top-level window.
"""
import tkinter as tk
import webbrowser
import customtkinter as ctk
import slg_account
import slg_db
from slg_image_resources import ResourceButton, ResourceLabel, ResourceScrollableFrame, cancel_widget_timers

AUX_PANEL_MODES = frozenset(('settings', 'tools', 'leaderboard'))


class RightPanelHost:
    """Cache three bounded pages, retaining their scroll and selection state."""
    def __init__(self, master):
        self.frame = ctk.CTkFrame(master, fg_color='transparent', corner_radius=0)
        self.frame.grid_rowconfigure(0, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)
        self.pages = {}
        self.active = None
        self.stamp = None
        self.generation = 0

    def show(self, key, stamp):
        if key not in AUX_PANEL_MODES:
            raise ValueError('Unknown right panel')
        if self.stamp != stamp:
            self.clear()
            self.stamp = stamp
        if self.active != key:
            self.generation += 1
        for page in self.pages.values():
            page.grid_remove()
        fresh = key not in self.pages
        if fresh:
            self.pages[key] = ctk.CTkFrame(self.frame, fg_color='transparent', corner_radius=0)
        page = self.pages[key]
        page.grid(row=0, column=0, sticky='nsew')
        self.frame.grid(row=0, column=1, sticky='nsew')
        self.active = key
        return page, fresh

    def hide(self):
        self.generation += 1
        self.active = None
        self.frame.grid_remove()

    def clear(self):
        self.generation += 1
        self.active = None
        for page in self.pages.values():
            cancel_widget_timers(page)
            page.destroy()
        self.pages.clear()

    def destroy(self):
        self.clear()
        cancel_widget_timers(self.frame)
        self.frame.destroy()


class RightPanelUI:
    def _right_panel_stamp(self):
        import slg_gui as g
        return (self._capture_identity_context(), g.CARD, g.TEXT, g.ACCENT)

    def _enter_aux_right_panel(self, key):
        if getattr(self, '_profile_workspace', None) is not None:
            self._close_profile_workspace(render_detail=False)
        self._close_workspace_menu()
        self._leave_collections_view()
        self._close_shop_page()
        self._set_shop_wide_layout(False)
        self.detail.grid_remove()
        host = getattr(self, '_aux_right_host', None)
        if host is None or not host.frame.winfo_exists():
            host = self._aux_right_host = RightPanelHost(self._content_body)
        previous = host.active
        page, fresh = host.show(key, self._right_panel_stamp())
        if previous != key:
            epoch = slg_page_scroll.begin(self, key, page, force=True)
            def reset_page():
                if (host.active != key or epoch != getattr(self, "_scroll_page_epoch", None)
                        or not page.winfo_exists()):
                    return
                def reset_children(widget):
                    canvas = getattr(widget, "_parent_canvas", None)
                    if canvas is not None:
                        canvas.yview_moveto(0.0)
                    for child in widget.winfo_children():
                        reset_children(child)
                reset_children(page)
            self.after_idle(reset_page)
            slg_page_scroll.finish(self)
        if fresh or previous != key:
            self._aux_panel_serial = getattr(self, '_aux_panel_serial', 0) + 1
        self._panel_mode = key
        if fresh:
            self._make_panel_feedback_header(key,page).pack(fill='x',padx=12,pady=(12,6))
        if key == 'leaderboard':
            self._leaderboard_generation = self._aux_panel_serial
        return page, fresh

    def _aux_right_panel_active(self, key):
        host = getattr(self, '_aux_right_host', None)
        return (getattr(self, '_panel_mode', None) == key and host is not None
                and host.active == key and host.frame.winfo_exists()
                and host.stamp == self._right_panel_stamp())

    def _leave_aux_right_panel(self, show_detail=True):
        if getattr(self, '_panel_mode', None) in AUX_PANEL_MODES:
            self._deactivate_panel_feedback(getattr(self, '_panel_mode'))
        host = getattr(self, '_aux_right_host', None)
        if host is not None and host.frame.winfo_exists():
            host.hide()
        if getattr(self, '_panel_mode', None) in AUX_PANEL_MODES:
            self._panel_mode = 'game'
        if show_detail and getattr(self, 'detail', None) is not None and self.detail.winfo_exists():
            self.detail.grid()

    def _invalidate_aux_right_panel(self):
        self._aux_panel_serial = getattr(self, '_aux_panel_serial', 0) + 1
        host = getattr(self, '_aux_right_host', None)
        if host is not None:
            host.destroy()
        self._aux_right_host = None
        self._leaderboard_window = None
        self._leaderboard_lists = {}
        self._leaderboard_states = {}
        self._leaderboard_generation = None
        self._leaderboard_pending_reads = {}

    def open_data_sources(self):
        return self.open_settings('about')

    def _load_leaderboard(self, board):
        if board not in ('points', 'wardrobe') or not self._aux_right_panel_active('leaderboard'):
            return
        state = getattr(self, '_leaderboard_states', {}).get(board)
        if state is None:
            return
        state.update(loading=True, error='')
        self._render_leaderboard_board(board)
        generation = self._leaderboard_generation
        pending = getattr(self, '_leaderboard_pending_reads', None)
        if pending is None:
            pending = self._leaderboard_pending_reads = {}
        if board in pending:
            return
        pending[board] = generation
        def fetch():
            try:
                result = slg_account.leaderboard(board, limit=30)
                if not isinstance(result, dict):
                    result = {'_panel_error': '服务器未返回排行榜数据'}
                else:
                    result = dict(result)
            except slg_account.AccountError as exc:
                result = {'_panel_error': str(exc)}
            except Exception:
                result = {'_panel_error': '读取排行榜时发生内部错误，请重试。'}
            result['_panel_generation'] = generation
            return result
        self._run_cloud_action('leaderboard_' + board, fetch)

    def open_settings(self, section=None):
        """Open the requested settings category, including in a cached panel."""
        import slg_gui as g
        self._report_ui_event('settings_open')
        win, fresh = self._enter_aux_right_panel('settings')
        if not fresh:
            if section is not None:
                self._settings_select_page(section)
            return
        body = ctk.CTkFrame(win, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=8, pady=8)
        nav = ctk.CTkFrame(body, fg_color='transparent')
        content = ctk.CTkFrame(body, fg_color='transparent')
        content.pack(side='left', fill='both', expand=True)
        page_names = (
            ('appearance', '外观', '外观'), ('alerts', '提醒与声音', '提醒'),
            ('api', 'API 配置', 'API 配置'), ('help', '帮助与反馈', '帮助'),
            ('about', '关于', '关于'),
        )
        pages = {}
        for key, _label, _compact_label in page_names:
            pages[key] = ResourceScrollableFrame(content, fg_color='transparent')
        nav_buttons = {}
        selected_page = {'key': getattr(self, '_settings_page', 'appearance')}
        populated = set()

        def select_page(key):
            if key not in pages:
                key = 'appearance'
            if key == 'api' and key not in populated:
                from slg_api_settings_ui import build_api_settings
                build_api_settings(self, pages[key])
                populated.add(key)
            changed = selected_page['key'] != key
            selected_page['key'] = key
            self._settings_page = key
            for page_key, page in pages.items():
                if page_key == key:
                    page.pack(fill='both', expand=True)
                else:
                    page.pack_forget()
            if changed:
                slg_page_scroll.begin(self, ("settings", key), pages[key], force=True)
                slg_page_scroll.finish(self)
            for page_key, button in nav_buttons.items():
                active = page_key == key
                button.configure(fg_color=g.ACCENT if active else 'transparent', text_color=g.ON_ACCENT if active else g.TEXT, hover_color=g.ACCENT_HOVER if active else g.CARD_HOVER)
        for key, label, compact_label in page_names:
            button = ResourceButton(nav, text=label, height=36, width=116, corner_radius=8, anchor='w', fg_color='transparent', text_color_disabled=g.MUTED, text_color=g.TEXT, hover_color=g.CARD, font=g.ui_font(size=12), command=lambda target=key: select_page(target))
            nav_buttons[key] = button
        from slg_settings_navigation import SettingsNavigationLayout
        self._settings_navigation = SettingsNavigationLayout(
            body, nav, content, nav_buttons, page_names, g.ui_font)
        wrapped_labels = []

        def page_heading(parent, title):
            ResourceLabel(parent, text=title, text_color=g.TEXT, anchor='w', font=g.ui_font(size=16, weight='bold')).pack(fill='x', padx=12, pady=(16, 10))

        def setting_card(parent, title):
            card = ctk.CTkFrame(parent, fg_color=g.CARD, corner_radius=10)
            card.pack(fill='x', padx=12, pady=(0, 10))
            ResourceLabel(card, text=title, text_color=g.TEXT, anchor='w', font=g.ui_font(size=13, weight='bold')).pack(fill='x', padx=12, pady=(11, 7))
            return card

        def info_note(parent, text):
            note = ctk.CTkFrame(parent, fg_color=g.CHIP, corner_radius=8)
            note.pack(fill='x', padx=10, pady=(8, 10))
            label = ResourceLabel(note, text=text, text_color=g.MUTED, anchor='w', justify='left', font=g.ui_font(size=11), wraplength=280)
            label.pack(fill='x', padx=10, pady=8)
            wrapped_labels.append(label)
            return label

        def action_row(parent, title, detail, command, emphasized=False):
            card = ctk.CTkFrame(parent, fg_color=g.CARD, corner_radius=10)
            card.pack(fill='x', padx=12, pady=(0, 10))
            button = ResourceButton(card, text=title, height=32, corner_radius=7, anchor='w', fg_color='transparent', text_color_disabled=g.MUTED, text_color=g.ACCENT if emphasized else g.TEXT, hover_color=g.CARD_HOVER, font=g.ui_font(size=13, weight='bold'), command=command)
            button.pack(fill='x', padx=8, pady=(7, 0))
            label = ResourceLabel(card, text=detail, text_color=g.MUTED, anchor='w', justify='left', font=g.ui_font(size=11), wraplength=280)
            label.pack(fill='x', padx=12, pady=(1, 9))
            wrapped_labels.append(label)
            return (card, button)
        appearance = pages['appearance']
        page_heading(appearance, '外观')
        theme_card = setting_card(appearance, '主题')
        switch = g.PaletteSegmentedButton(theme_card, values=[g._THEME_LABELS[m] for m in ('light', 'dark', 'system')], height=34, corner_radius=8, fg_color=g.CHIP, selected_color=g.ACCENT, selected_hover_color=g.ACCENT, unselected_color=g.CHIP, unselected_hover_color=g.CARD_HOVER, text_color=g.TEXT, font=g.ui_font(size=13), command=lambda label: self._on_theme_pick(label))
        switch.pack(fill='x', padx=10, pady=(0, 0))
        switch.set(g._THEME_LABELS[self.theme_mode])
        info_note(theme_card, '「跟随系统」每 5 秒采样一次 Windows 的浅色/深色设置，所以会有一小段延迟；手动选浅色或深色则会被记住。')
        scale_card = setting_card(appearance, '界面尺寸')
        scale_values = [entry['label'] for entry in g.UI_SCALE_PROFILES.values()]
        scale_menu = ctk.CTkOptionMenu(scale_card, values=scale_values, height=34, corner_radius=8, fg_color=g.CHIP, button_color=g.CHIP, button_hover_color=g.CARD_HOVER, text_color=g.TEXT, font=g.ui_font(size=12), command=self._pick_ui_scale_profile)
        scale_menu.pack(fill='x', padx=10, pady=(0, 0))
        scale_menu.set(g.UI_SCALE_PROFILES[self.ui_scale_profile]['label'])
        info_note(scale_card, '只调整本软件窗口和控件，不会修改 Windows 显示分辨率。分辨率档位是界面缩放预设；「自动」沿用系统 DPI。')
        font_card = setting_card(appearance, '字体大小')
        font_menu = ctk.CTkOptionMenu(font_card, values=[entry['label'] for entry in g.FONT_SCALE_PROFILES.values()], height=34, corner_radius=8, fg_color=g.CHIP, button_color=g.CHIP, button_hover_color=g.CARD_HOVER, text_color=g.TEXT, font=g.ui_font(size=12), command=self._pick_font_scale)
        font_menu.pack(fill='x', padx=10, pady=(0, 0))
        font_menu.set(g.FONT_SCALE_PROFILES[self.font_scale_profile]['label'])
        info_note(font_card, '只放大文字，窗口大小和封面尺寸不变。整体都小就调上面的界面尺寸，只有字看不清就用这一项。')
        motion_card = setting_card(appearance, '交互动效')
        motion_var = ctk.BooleanVar(value=self.reduced_motion)

        def set_motion():
            self.reduced_motion = bool(motion_var.get())
            if self._micro_motion is not None:
                self._micro_motion.set_reduced(self.reduced_motion)
            slg_db.set_pref(self.conn, 'ui.reduced_motion', '1' if self.reduced_motion else '0')
        ctk.CTkCheckBox(motion_card, text='减少动效', variable=motion_var, command=set_motion, font=g.ui_font(size=12), text_color=g.TEXT, fg_color=g.ACCENT).pack(anchor='w', padx=10, pady=8)
        info_note(motion_card, '停止持续装饰、彩带和摇窗，保留等待提示和最终结果。')
        sensitive_var = ctk.BooleanVar(value=slg_db.get_pref(self.conn, 'vndb.hide_sensitive_covers') == '1')
        ctk.CTkCheckBox(motion_card, text='隐藏VNDB敏感封面', variable=sensitive_var, command=lambda: self._set_sensitive_covers(bool(sensitive_var.get())), font=g.ui_font(size=12), text_color=g.TEXT, fg_color=g.ACCENT).pack(anchor='w', padx=10, pady=8)
        alerts = pages['alerts']
        page_heading(alerts, '提醒与声音')
        sound_card = setting_card(alerts, '每日抽奖音效')
        sound = ctk.CTkSwitch(sound_card, text='每日抽奖的转盘声与中奖声', onvalue='1', offvalue='0', font=g.ui_font(size=12), text_color=g.TEXT, progress_color=g.ACCENT, command=lambda: slg_db.set_pref(self.conn, 'sound.lottery', sound.get()))
        sound.pack(fill='x', padx=10, pady=(0, 11))
        if self._lottery_sound_on():
            sound.select()
        help_page = pages['help']
        page_heading(help_page, '帮助与反馈')
        action_row(help_page, '查看本地错误日志…', '只读查看最近错误，可手动刷新或打开文件夹；日志不会自动上传。', self.open_error_log)
        action_row(help_page, '查看新手引导…', '重新打开首次启动时的那份功能简介和使用说明。', self._show_welcome)
        action_row(help_page, '意见反馈…', '把问题、建议或资料纠错私下发给开发者；被采纳后才可能获得奖励。', self.open_feedback)
        action_row(help_page, 'Bangumi本机授权…', '可选：在Bangumi官网生成访问令牌后填入，仅加密保存在本机。', self.open_bangumi_authorization)
        action_row(help_page, '打开本机数据目录', slg_db.app_dir(), self._open_data_dir)
        action_row(help_page, '打开封面缓存目录', slg_db.covers_dir(), lambda: self._open_storage_dir(slg_db.covers_dir()))
        about_page = pages['about']
        page_heading(about_page, '关于')
        source_card = setting_card(about_page, '数据来源')
        for title, url, note in (
            ('Dikgames', 'https://dikgames.com/', '欧美目录的资料来源；作品、评分及更新以所标来源事实为准。'),
            ('F95公开镜像', 'https://f95zone.to.it/', '目前使用公开镜像，非F95zone官方接口；发现范围与访问条件有限。'),
            ('DLsite', 'https://www.dlsite.com/', '作品编号、社团、语言及封面来自对应作品资料；网页语言不代表作品支持语言。'),
            ('VNDB', 'https://vndb.org/', '视觉小说资料与原生评分独立展示；目录开放范围取决于当前渠道与分发状态。'),
        ):
            ResourceButton(source_card, text=title + ' ↗', height=44, anchor='w',
                fg_color='transparent', hover_color=g.CARD_HOVER, text_color=g.ACCENT,
                command=lambda target=url: webbrowser.open(target)).pack(fill='x', padx=10, pady=(5, 0))
            source_note = ResourceLabel(source_card, text=note, wraplength=260, justify='left',
                anchor='w', text_color=g.MUTED, font=g.ui_font(size=12))
            source_note.pack(fill='x', padx=12, pady=(0, 8))
            wrapped_labels.append(source_note)
        action_row(about_page, '关于本软件…', '版本信息、致谢、检查软件更新、GitHub 主页、交流群与数据目录。', self.open_about)

        def resize_setting_text(event):
            try:
                scale = float(ctk.ScalingTracker.get_widget_scaling(content))
            except (AttributeError, TypeError, ValueError, tk.TclError):
                scale = 1.0
            wrap = max(120, int(event.width / max(scale, 0.1)) - 56)
            for label in wrapped_labels:
                label.configure(wraplength=wrap)
        content.bind('<Configure>', resize_setting_text, add='+')
        self._settings_navigation.request_layout()
        self._settings_pages = pages
        self._settings_select_page = select_page
        select_page(section or selected_page['key'])

    def open_tools(self):
        """Keep the tools directly reachable in one scrollable group."""
        import slg_gui as g
        self._report_ui_event('tools_open')
        win, fresh = self._enter_aux_right_panel('tools')
        if not fresh:
            for button in getattr(self, '_tools_buttons', ()):
                button.configure(state='disabled' if self.busy else 'normal')
            return
        self._tools_buttons = []
        ResourceLabel(win, text='选择需要的工具；扫描与游戏维护请前往本地游戏库。', text_color=g.MUTED, font=g.ui_font(size=12), wraplength=260, justify='left', anchor='w').pack(fill='x', padx=16, pady=(10, 2))
        body = ResourceScrollableFrame(win, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=0, pady=(2, 8))
        routine = (('已隐藏…', '查看与恢复已隐藏的游戏；不会删除磁盘文件。', self.open_hidden_games, None, 'hidden_games'), ('备份与恢复…', '把评分、评论、收藏、标签排除导出成文件，或从文件恢复。', self.open_backup, None, 'backup_restore'))
        advanced = (('游戏汉化工具…', '游戏是英文的？这里有搭配使用的翻译工具。', self.open_translation_tools, None, 'translation_tools'), ('标签译名…', '给标签写中文名。改完列表和筛选条立刻跟着变。', self.open_tag_editor, None, 'tag_translations'), ('偏好权重…', '从你的五星评分里算出来的标签倾向，正数是你喜欢的。', self.open_weights, None, 'preference_weights'))
        state = 'disabled' if self.busy else 'normal'

        def section(title, entries, parent):
            ResourceLabel(parent, text=title, text_color=g.ACCENT, font=g.ui_font(size=12, weight='bold'), anchor='w').pack(fill='x', padx=16, pady=(8, 1))
            for label, blurb, command, on_right, tool_id in entries:
                card = ctk.CTkFrame(parent, fg_color=g.CARD, corner_radius=8, border_width=1, border_color=g.CHIP)
                card.pack(fill='x', padx=16, pady=(4, 0))
                if tool_id == 'sync_maintenance':
                    open_tool = command
                else:
                    open_tool = lambda c=command, tid=tool_id: (self._report_ui_event('tool_open', tid), c())

                def invoke_tool(c=open_tool):
                    if not self.busy:
                        c()
                button = ResourceButton(card, text=label, height=44, corner_radius=6, anchor='w', fg_color='transparent', text_color_disabled=g.MUTED, text_color=g.TEXT, hover_color=g.CARD_HOVER, font=g.ui_font(size=13), state=state, command=invoke_tool)
                button.pack(fill='x', padx=5, pady=(4, 0))
                self._tools_buttons.append(button)
                description = ResourceLabel(card, text=blurb, text_color=g.MUTED, font=g.ui_font(size=11), justify='left', anchor='w', wraplength=260)
                description.pack(fill='x', padx=13, pady=(0, 6))
                if on_right is not None and state == 'normal':

                    def choose_scan_root(event=None, c=on_right, tid=tool_id):
                        self._report_ui_event('tool_open', tid)
                        c()
                        return 'break'
                    for widget in (card, button, description):
                        widget.bind('<Button-3>', choose_scan_root)
        section('常用', routine + advanced, body)

    def open_leaderboard(self):
        """Keep the header fixed and let only the ranked list scroll."""
        import slg_gui as g
        win, fresh = self._enter_aux_right_panel('leaderboard')
        if not fresh:
            for board, state in self._leaderboard_states.items():
                if state.get('loading'):
                    self._load_leaderboard(board)
            return
        self._leaderboard_window = win
        ResourceLabel(win, text='每榜仅显示前 30 名（不含管理员）；只展示昵称和榜单数值。', text_color=g.MUTED, font=g.ui_font(size=11), wraplength=260, justify='left').pack(anchor='w', padx=16, pady=(12, 7))
        tabs = ctk.CTkTabview(win)
        tabs.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        self._leaderboard_tabs = tabs
        self._leaderboard_states = {'points': {'loading': True, 'error': '', 'entries': []}, 'wardrobe': {'loading': True, 'error': '', 'entries': []}}
        self._leaderboard_lists = {}
        for board, title in (('points', '云端积分榜'), ('wardrobe', '装扮榜')):
            tab = tabs.add(title)
            header = ctk.CTkFrame(tab, fg_color='transparent')
            header.pack(fill='x', padx=4, pady=(4, 2))
            ResourceLabel(header, text='按当前云端积分排序' if board == 'points' else '按已拥有装扮总数排序（头衔、头像框、名片框）', text_color=g.MUTED, font=g.ui_font(size=12), wraplength=210, justify='left').pack(side='left', fill='x', expand=True)
            ResourceButton(header, text='刷新', width=62, height=40, corner_radius=7, fg_color=g.CHIP, text_color_disabled=g.MUTED, text_color=g.TEXT, hover_color=g.CARD_HOVER, font=g.ui_font(size=12), command=lambda b=board: self._load_leaderboard(b)).pack(side='right')
            listing = ResourceScrollableFrame(tab, fg_color='transparent')
            listing.pack(fill='both', expand=True, padx=2, pady=(0, 4))
            self._leaderboard_lists[board] = listing
            self._render_leaderboard_board(board)
        self._load_leaderboard('points')
        self._load_leaderboard('wardrobe')

    def _render_leaderboard_board(self, board):
        import slg_gui as g
        listing = getattr(self, '_leaderboard_lists', {}).get(board)
        state = getattr(self, '_leaderboard_states', {}).get(board)
        if listing is None or state is None:
            return
        try:
            if not listing.winfo_exists():
                return
            for child in listing.winfo_children():
                cancel_widget_timers(child)
                child.destroy()
        except tk.TclError:
            return
        if state.get('loading') and (not state.get('entries')):
            ResourceLabel(listing, text='正在读取排行榜…', text_color=g.MUTED, font=g.ui_font(size=12)).pack(anchor='w', padx=8, pady=12)
            return
        if state.get('error') and (not state.get('entries')):
            ResourceLabel(listing, text='排行榜暂时无法加载\n' + state['error'], text_color=g.DANGER_TEXT, font=g.ui_font(size=11), wraplength=330, justify='left').pack(anchor='w', padx=8, pady=(14, 6))
            ResourceButton(listing, text='重试', height=28, fg_color=g.CHIP, text_color_disabled=g.MUTED, text_color=g.TEXT, hover_color=g.CARD_HOVER, command=lambda b=board: self._load_leaderboard(b)).pack(fill='x', padx=8, pady=(0, 8))
            return
        entries = state.get('entries') or []
        if not entries:
            ResourceLabel(listing, text='暂时还没有上榜数据。', text_color=g.MUTED, font=g.ui_font(size=11)).pack(anchor='w', padx=8, pady=12)
            return
        for entry in entries:
            row = ctk.CTkFrame(listing, fg_color=g.CARD, corner_radius=8)
            row.pack(fill='x', padx=4, pady=4)
            row.grid_columnconfigure(1, weight=1)
            rank = int(entry.get('rank') or 0)
            rank_color = {1: '#f0c56a', 2: '#c3cbd5', 3: '#c98663'}.get(rank, g.MUTED)
            ResourceLabel(row, text='%02d' % rank if rank > 0 else '—', width=34,
                text_color=rank_color, font=g.ui_font(size=14, weight='bold')).grid(
                    row=0, column=0, rowspan=2, sticky='ns', padx=(8, 4), pady=8)
            nickname = str(entry.get('nickname') or '未设置昵称')[:32]
            name = ResourceLabel(row, text=nickname, text_color=g.TEXT,
                font=g.ui_font(size=13), anchor='w', justify='left', wraplength=210)
            name.grid(row=0, column=1, sticky='ew', padx=4, pady=(9, 2))
            score = int(entry.get('score') or 0)
            score_text = '%s 积分' % score if board == 'points' else '%s 件装扮' % score
            ResourceLabel(row, text=score_text, text_color=g.ACCENT,
                font=g.ui_font(size=12, weight='bold')).grid(
                    row=1, column=1, sticky='w', padx=4, pady=(0, 9))
            ResourceButton(row, text='查看资料', width=84, height=44, fg_color=g.CHIP,
                text_color=g.TEXT, text_color_disabled=g.MUTED, hover_color=g.CARD_HOVER,
                font=g.ui_font(size=12), command=lambda e=dict(entry): self._show_public_author_card(e)).grid(
                    row=2, column=1, sticky='ew', padx=(4, 10), pady=(0, 8))
            def fit_name(event, label=name):
                try:
                    scale = float(ctk.ScalingTracker.get_widget_scaling(label))
                    wrap = max(60, int(event.width / max(scale, 0.1)) - 70)
                    if int(label.cget('wraplength')) != wrap:
                        label.configure(wraplength=wrap)
                except tk.TclError:
                    return
            row.bind('<Configure>', fit_name, add='+')
        if state.get('error'):
            ResourceLabel(listing, text='刷新失败，显示上次已加载的数据。', text_color=g.MUTED, font=g.ui_font(size=10)).pack(anchor='w', padx=8, pady=5)

    def _leaderboard_action_result(self, board, result, error):
        import slg_gui as g
        if not isinstance(result, dict):
            return
        generation = result.get('_panel_generation')
        pending = getattr(self, '_leaderboard_pending_reads', {})
        if pending.get(board) == generation:
            pending.pop(board, None)
        if generation != getattr(self, '_leaderboard_generation', None):
            if self._aux_right_panel_active('leaderboard'):
                self._load_leaderboard(board)
            return
        if not self._aux_right_panel_active('leaderboard'):
            return
        error = result.get('_panel_error') or error
        states = getattr(self, '_leaderboard_states', {})
        state = states.get(board)
        if state is None:
            return
        state['loading'] = False
        if error:
            state['error'] = str(error)
        elif isinstance(result, dict):
            entries = result.get('entries')
            if result.get('board') != board or not isinstance(entries, list):
                state['error'] = '服务器返回的排行榜格式不正确'
            else:
                cleaned = []
                for entry in entries[:30]:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        cleaned.append({'rank': int(entry.get('rank') or len(cleaned) + 1), 'nickname': str(entry.get('nickname') or '未设置昵称'), 'score': max(0, int(entry.get('score') or 0)), 'public_id': str(entry.get('public_id') or ''), 'profile_visible': entry.get('profile_visible')})
                    except (TypeError, ValueError):
                        continue
                state['entries'] = cleaned
                state['error'] = ''
        else:
            state['error'] = '服务器未返回排行榜数据'
        self._render_leaderboard_board(board)
