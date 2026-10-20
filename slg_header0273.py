"""Persistent app header. All projections are local and identity-scoped."""
from dataclasses import replace
from slg_local_avatar import identity_key
import tkinter as tk
import customtkinter as ctk
from slg_image_resources import ResourceButton, ResourceLabel
from slg_account_chip import AccountChip, project_account


class HeaderUI:
    def _build_account_header(self, parent):
        import slg_gui as g
        header = self._account_header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.grid_columnconfigure(0, minsize=112, uniform="header-sides")
        header.grid_columnconfigure(1, weight=1)
        header.grid_columnconfigure(2, minsize=112, uniform="header-sides")
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=1, sticky="ew")
        titles.grid_columnconfigure(0, weight=1)
        self.workspace_title = ResourceLabel(titles, text="全部游戏库", anchor="center",
            text_color=g.TEXT, font=g.ui_font(size=22, weight="bold"))
        self.workspace_title.grid(row=0,column=0,sticky="ew")
        self.workspace_subtitle = ResourceLabel(titles, text="筛选作品范围，再按你的偏好排序",
            anchor="center",text_color=g.MUTED,font=g.ui_font(size=11))
        self.workspace_subtitle.grid(row=1,column=0,sticky="ew",pady=(3,0))
        actions=ctk.CTkFrame(header,fg_color="transparent")
        actions.grid(row=0,column=2,sticky="e")
        self.profile_btn=self.leaderboard_btn=self.shop_btn=None
        self._account_chip_context=self._capture_identity_context()
        self._build_notification_bell(actions).pack(side="left", padx=(0, 8))
        self._account_chip=AccountChip(actions,provider=self._account_summary,
            on_profile=self.open_profile,on_show=self._animate_account_popup,
            screen_bounds=self._usable_screen_bounds,
            palette=dict(card=g.CARD,hover=g.CARD_HOVER,text=g.TEXT,muted=g.MUTED,
                         accent=g.ACCENT,accent_hover=g.ACCENT_HOVER,on_accent=g.ON_ACCENT))
        self._account_chip.pack(side="left")
        self._reload_local_avatar()

    def _account_summary(self):
        import slg_gui as g
        context=getattr(self,"_account_chip_context",{})
        account=context.get("account_id")
        cache=getattr(self,"_cloud_me",{})
        valid=isinstance(cache,dict) and cache.get("account_id")==account
        appearances=self._equipped_appearance_map() if valid else {}
        def appearance_name(slot):
            item_id=appearances.get(slot)
            if not item_id: return "未装备"
            item=self._appearance_item(item_id)
            return item.get("name",item_id) if isinstance(item,dict) else item_id
        title_id=cache.get("equipped_title") if valid else None
        title=g.slg_titles.title_by_id(title_id) or {}
        title_name=(title.get("name") if isinstance(title,dict) else None) or title_id
        avatar=None
        summary = project_account(account_id=account,generation=context.get("generation",0),
            admin=context.get("admin",False),snapshot=cache,
            balance=getattr(self,"_cloud_balance",None),
            balance_account_id=getattr(self,"_cloud_balance_account_id",None),
            title_name=title_name,avatar_frame_name=appearance_name("avatar_frame"),
            comment_frame_name=appearance_name("comment_frame"),developer_avatar=avatar,
            stale=bool(getattr(self,"_account_summary_stale",False)))
        if getattr(self, "_local_avatar_pil", None) is None:
            return summary
        expected = identity_key(account, context.get("admin", False))
        image = (self._own_avatar_image(44)
                 if expected == getattr(self, "_local_avatar_loaded_key", None) else None)
        return replace(summary, avatar_image=image)

    def _schedule_account_chip_refresh(self, stale=None):
        if stale is not None: self._account_summary_stale=stale
        if getattr(self,"_account_chip_refresh_job",None) is not None: return
        def refresh():
            self._account_chip_refresh_job=None
            chip=getattr(self,"_account_chip",None)
            if chip is not None and chip.winfo_exists():
                self._account_chip_context=self._capture_identity_context()
                self._reload_local_avatar()
                chip.refresh()
        self._account_chip_refresh_job=self.after_idle(refresh)

    def _close_account_header(self):
        self._close_notifications()
        job=getattr(self,"_account_chip_refresh_job",None)
        if job is not None:
            try: self.after_cancel(job)
            except tk.TclError: pass
        self._account_chip_refresh_job=None
        chip=getattr(self,"_account_chip",None)
        if chip is not None: chip.dispose()
        self._account_chip=None

    def _animate_account_popup(self, popup):
        self._close_notifications()
        motion=getattr(self,"_micro_motion",None)
        if motion is None or self.reduced_motion: return
        target_x,target_y=popup._slg_anchor_position
        motion.play("account-popup",popup,
            lambda p: popup.geometry("+%d+%d"%(target_x,target_y+round(5*(1-p)))),kind="popover")

    def _set_header_title(self,title,subtitle=""):
        self._close_workspace_menu()
        # Returning from a custom workspace must restore catalogue chrome even
        # when source/origin/filter values are identical to the previous visit.
        self._catalogue_controls_sig = None
        label=getattr(self,"workspace_title",None)
        if label is not None and label.winfo_exists():
            label.configure(text=title)
            self.workspace_subtitle.configure(text=subtitle)
            self._animate_nav_selection(title)

    @staticmethod
    def _blend_color(a,b,p):
        values=[round(int(a[i:i+2],16)*(1-p)+int(b[i:i+2],16)*p) for i in (1,3,5)]
        return "#"+"".join(f"{v:02x}" for v in values)

    def _attach_workspace_motion(self):
        import slg_gui as g
        motion=self._micro_motion
        buttons=list(self.library_action_buttons)
        buttons+=list(getattr(self,"_sidebar_nav_buttons",()))
        for button in buttons:
            base=g.SIDEBAR if getattr(button,"_slg_sidebar_nav",False) else g.CARD
            button.configure(border_width=1)
            def paint(p,button=button,base=base):
                if button is getattr(self,"_nav_selected_widget",None):
                    button.configure(border_color=g.ACCENT)
                else:
                    button.configure(border_color=self._blend_color(base,g.ACCENT,p))
            paint(0)
            motion.attach_button(button,paint)

    def _animate_nav_selection(self, text=None):
        import slg_gui as g
        motion=getattr(self,"_micro_motion",None)
        if motion is None: return
        if text is None:
            text=("收藏夹" if self._workspace_mode=="collections" else
                  "本地游戏库" if self.origin in ("local","user") else None)
        button=next((b for b in getattr(self,"_sidebar_nav_buttons",()) if b.cget("text")==text),None)
        if button is None and text is None:
            button=self.view_buttons.get(self.view)
        old=getattr(self,"_nav_selected_widget",None)
        if old is not None and old is not button and old.winfo_exists():
            old.configure(border_color=g.SIDEBAR)
        self._nav_selected_widget=button
        if button is not None:
            motion.play("navigation-selection",button,
                lambda p: button.configure(border_color=self._blend_color(g.SIDEBAR,g.ACCENT,p)),kind="nav")

    def _success_highlight(self):
        import slg_gui as g
        button=getattr(self,"sync_btn",None)
        motion=getattr(self,"_micro_motion",None)
        if motion is not None and button is not None and button.winfo_exists():
            motion.play("operation-success",button,
                lambda p: button.configure(fg_color=self._blend_color(g.ACCENT_HOVER,g.ACCENT,p)),kind="success")

    def _leave_panel_workspace(self):
        if getattr(self,"_panel_mode",None) not in ("shop","profile","comments"): return
        self._close_shop_page()
        self._set_shop_wide_layout(False)
        self._panel_mode="game"
        self.selected=None
        self._detail_sig=None
        self._render_detail()
