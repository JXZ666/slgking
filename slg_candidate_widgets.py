"""Shared lookup controls, wrapped candidates and validated official links."""
import math
import tkinter as tk
import webbrowser
import customtkinter as ctk
from slg_image_resources import ResourceButton, ResourceLabel, ResourceScrollableFrame
from slg_source_registry import SOURCES, official_url, valid_identity_url


def logical_wrap_width(physical_width, scale, padding=0, minimum=40):
    if not isinstance(scale,(int,float)) or not math.isfinite(scale) or scale<=0:
        raise ValueError('Invalid widget scale')
    return max(minimum,int(float(physical_width)/scale)-int(padding))


def official_candidate_url(candidate):
    if not isinstance(candidate,dict):
        raise ValueError('No selected official work')
    source,identity=candidate.get('source'),candidate.get('external_id')
    url=official_url(source,identity)
    supplied=candidate.get('source_url')
    if supplied and not valid_identity_url(source,identity,supplied):
        raise ValueError('The link is not the selected official work')
    # Copy/open a canonical work link, never a query containing tracking data.
    return url


class WrappedLabel(ResourceLabel):
    """Wrap against a fixed viewport; CTk receives unscaled logical units."""
    def __init__(self,master,*,viewport=None,padding=0,**kwargs):
        kwargs.setdefault('width',1)
        kwargs.setdefault('wraplength',140)
        kwargs.setdefault('justify','left')
        kwargs.setdefault('anchor','w')
        super().__init__(master,**kwargs)
        self._wrap_viewport=viewport or master
        self._wrap_padding=padding
        self._wrap_binding=self._wrap_viewport.bind('<Configure>',self._fit_width,add='+')
        self._fit_width()
    def _fit_width(self,event=None):
        if not self.winfo_exists():return
        width=getattr(event,'width',self._wrap_viewport.winfo_width())
        if width<=1:return
        wrap=logical_wrap_width(width,self._get_widget_scaling(),self._wrap_padding)
        if self.cget('wraplength')!=wrap:self.configure(wraplength=wrap)
    def destroy(self):
        try:
            if self._wrap_binding and self._wrap_viewport.winfo_exists():
                self._wrap_viewport.unbind('<Configure>',self._wrap_binding)
        except tk.TclError:
            pass
        self._wrap_binding=None
        super().destroy()


def enable_button_keys(button):
    """Include a text button in Tab navigation and honor its disabled state."""
    button._text_label.configure(takefocus=True)

    def activate(_event):
        button.invoke()
        return 'break'

    button.bind('<Return>', activate)
    button.bind('<space>', activate)


class LookupBar(ctk.CTkFrame):
    def __init__(self, master, source_labels, on_search, *, color, font,
                 action_text='查询资料'):
        super().__init__(master, fg_color='transparent', width=1)
        self.grid_columnconfigure(1, weight=1)
        for column, label in enumerate(('资料来源', '名称、编号或官方链接')):
            ResourceLabel(self, text=label, text_color=color('TEXT'), font=font(size=12),
                          anchor='w').grid(row=0, column=column, sticky='w', pady=(0, 3))
        self.source = ctk.CTkOptionMenu(
            self, values=source_labels, width=110, height=36,
            fg_color=color('CHIP'), button_color=color('CHIP'),
            button_hover_color=color('CARD_HOVER'), text_color=color('TEXT'),
            text_color_disabled=color('MUTED'), dropdown_fg_color=color('CARD'),
            dropdown_text_color=color('TEXT'), font=font(size=13))
        self.source.grid(row=1, column=0, sticky='ew', padx=(0, 8))
        self.term = tk.StringVar(master=self)
        self.query = ctk.CTkEntry(self, textvariable=self.term, width=1, height=36,
                                  fg_color=color('CARD'), text_color=color('TEXT'),
                                  font=font(size=13))
        self.query.grid(row=1, column=1, sticky='ew')
        self.search = ResourceButton(
            self, text=action_text, width=94, height=36, command=on_search,
            fg_color=color('ACCENT'), hover_color=color('ACCENT_HOVER'),
            text_color=color('ON_ACCENT'), text_color_disabled=color('MUTED'),
            font=font(size=13))
        self.search.grid(row=1, column=2, padx=(8, 0))
        enable_button_keys(self.search)

        def submit(_event):
            self.search.invoke()
            return 'break'

        self.query.bind('<Return>', submit)

    def set_busy(self, busy):
        state = 'disabled' if busy else 'normal'
        self.source.configure(state=state)
        self.query.configure(state=state)
        self.search.configure(state=state)


class LookupPanes(ctk.CTkFrame):
    def __init__(self, master, *, color, font):
        super().__init__(master, fg_color='transparent')
        self.layout = {'wide': None}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        style = dict(fg_color=color('CARD'), label_text_color=color('TEXT'),
                     label_anchor='w', label_font=font(size=13), height=120, width=1)
        self.results = ResourceScrollableFrame(self, label_text='候选作品', **style)
        self.details = ResourceScrollableFrame(self, label_text='作品详情', **style)
        self.bind('<Configure>', self.fit_width, add='+')
        self.fit_width()

    def fit_width(self, _event=None):
        if not self.winfo_exists():
            return
        wide = self.winfo_width() / self._get_widget_scaling() >= 760
        if wide == self.layout['wide']:
            return
        self.layout['wide'] = wide
        self.grid_columnconfigure(1, weight=2 if wide else 0)
        self.grid_rowconfigure(1, weight=0 if wide else 2)
        self.results.grid(row=0, column=0, sticky='nsew',
                          padx=(0, 10) if wide else 0, pady=0 if wide else (0, 8))
        self.details.grid(row=0 if wide else 1, column=1 if wide else 0, sticky='nsew')


class CandidateRow(ctk.CTkFrame):
    """Full wrapped title, separate identity and keyboard-operable selection."""
    def __init__(self,master,candidate,on_select,*,color,font,alive=lambda:True,
                 enabled=lambda:True,action_text='选择资料'):
        super().__init__(master,fg_color=color('CHIP'),corner_radius=8,width=1)
        self.candidate=dict(candidate)
        self.grid_columnconfigure(0,weight=1)
        self._alive,self._enabled=alive,enabled
        self._on_select=on_select
        self.title_label=WrappedLabel(self,viewport=self,padding=112,
            text=str(candidate.get('title') or '未命名作品'),text_color=color('TEXT'),font=font(size=13))
        self.title_label.grid(row=0,column=0,sticky='ew',padx=(10,6),pady=(9,3))
        source=SOURCES.get(candidate.get('source'))
        identity=(source.label if source else str(candidate.get('source') or ''))+' · '+str(candidate.get('external_id') or '')
        evidence = [str(candidate[key]) for key in ('developer', 'release_date') if candidate.get(key)]
        self._identity_text = identity + ('\n' + ' · '.join(evidence) if evidence else '')
        self.identity_label=WrappedLabel(self,viewport=self,padding=112,text=self._identity_text,
            text_color=color('MUTED'),font=font(size=12))
        self.identity_label.grid(row=1,column=0,sticky='ew',padx=(10,6),pady=(0,9))
        self.choose_button=ResourceButton(self,text=action_text,width=88,height=40,
            fg_color=color('CARD'),hover_color=color('CARD_HOVER'),text_color=color('TEXT'),
            text_color_disabled=color('MUTED'),font=font(size=12),command=self.activate)
        self.choose_button.grid(row=0,column=1,rowspan=2,sticky='ne',padx=(0,8),pady=8)
        self.choose_button._text_label.configure(takefocus=True)
        self.choose_button.bind('<Return>',lambda _event:self.activate())
        self.choose_button.bind('<space>',lambda _event:self.activate())
        self._selected_color = color('ACCENT')

    def set_selected(self, selected):
        self.configure(border_width=int(selected), border_color=self._selected_color)
        self.identity_label.configure(text=('已选择 · ' if selected else '') + self._identity_text)

    def activate(self):
        if self._alive() and self._enabled() and self.choose_button.cget('state')!='disabled':
            self._on_select(dict(self.candidate))
        return 'break'


class OfficialLinkPanel(ctk.CTkFrame):
    """Readonly selectable URL and explicit copy/open actions; no hover network."""
    def __init__(self,master,*,color,font,alive=lambda:True):
        super().__init__(master,fg_color='transparent',width=1)
        self._alive=alive
        self._url=''
        self._identity=None
        self.grid_columnconfigure((0,1),weight=1,uniform='official-link-actions')
        self.value=tk.StringVar(value='')
        self.entry=ctk.CTkEntry(self,textvariable=self.value,state='readonly',height=36,
            width=1,fg_color=color('CARD'),text_color=color('TEXT'),font=font(size=12))
        self.entry.grid(row=0,column=0,columnspan=2,sticky='ew',pady=(0,6))
        self.entry.bind('<Control-a>',self._select_all)
        self.copy_button=ResourceButton(self,text='复制链接',height=40,width=1,
            fg_color=color('CHIP'),hover_color=color('CARD_HOVER'),text_color=color('TEXT'),
            text_color_disabled=color('MUTED'),font=font(size=12),command=self.copy)
        self.copy_button.grid(row=1,column=0,sticky='ew',padx=(0,4))
        self.open_button=ResourceButton(self,text='浏览器打开',height=40,width=1,
            fg_color=color('CHIP'),hover_color=color('CARD_HOVER'),text_color=color('TEXT'),
            text_color_disabled=color('MUTED'),font=font(size=12),command=self.open)
        self.open_button.grid(row=1,column=1,sticky='ew',padx=(4,0))
        for button, action in ((self.copy_button,self.copy),(self.open_button,self.open)):
            button._text_label.configure(takefocus=True)
            button.bind('<Return>',lambda _event,callback=action:(callback(),'break')[-1])
            button.bind('<space>',lambda _event,callback=action:(callback(),'break')[-1])
        self.clear()
    def _select_all(self,_event=None):
        self.entry.select_range(0,'end');return 'break'
    def clear(self):
        self._url='';self._identity=None
        try:
            if not self.winfo_exists():return
        except tk.TclError:
            return
        self.value.set('')
        self.copy_button.configure(state='disabled');self.open_button.configure(state='disabled')
    def set_candidate(self,candidate):
        self.clear()
        try:url=official_candidate_url(candidate)
        except (ValueError,TypeError,KeyError):return False
        try:
            if not self.winfo_exists() or not self._alive():return False
        except tk.TclError:
            return False
        self._identity=dict(candidate);self._url=url;self.value.set(url)
        self.entry.xview_moveto(0)
        self.copy_button.configure(state='normal');self.open_button.configure(state='normal')
        return True
    def _current_url(self):
        try:
            if not self.winfo_exists() or not self._alive() or not self._identity:return ''
        except tk.TclError:
            return ''
        try:return official_candidate_url(self._identity)
        except (ValueError,TypeError,KeyError):return ''
    def copy(self):
        url=self._current_url()
        if url and self.copy_button.cget('state')!='disabled':
            self.clipboard_clear();self.clipboard_append(url)
    def open(self):
        url=self._current_url()
        if url and self.open_button.cget('state')!='disabled':webbrowser.open_new_tab(url)
