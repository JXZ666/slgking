"""A centred wrapping catalogue choice; stable segmented-menu compatibility."""
import customtkinter as ctk

class CatalogueSwitch(ctk.CTkFrame):
    def __init__(self, master, values, command, font=None, **kwargs):
        import slg_gui as g
        super().__init__(master, fg_color='transparent')
        self._state='normal';self._value='';self._command=command
        self._flow=g.FlowFrame(self, centered=True, fg_color='transparent', gap_x=8, gap_y=8)
        self._flow.pack(fill='x')
        self._choices={}
        for label in values:
            button=g.ResourceButton(self._flow,text=label,width=max(100,len(label)*14+28),height=40,
                fg_color=g.CARD,text_color=g.TEXT,text_color_disabled=g.MUTED,hover_color=g.CARD_HOVER,
                corner_radius=8,font=font or g.ui_font(size=14),
                command=lambda value=label:self._choose(value))
            self._choices[label]=button
        self._flow.set_items(self._choices.values())
    def _choose(self,label):
        if self._state=='disabled':return
        self.set(label)
        self._command(label)
    def set(self,label):
        import slg_gui as g
        if label not in self._choices:raise ValueError('Unknown catalogue label')
        self._value=label
        for value,button in self._choices.items():
            active=value==label
            button.configure(fg_color=g.ACCENT if active else g.CARD,
                hover_color=g.ACCENT_HOVER if active else g.CARD_HOVER,
                text_color=g.ON_ACCENT if active else g.TEXT,
                text_color_disabled=g.ON_ACCENT if active else g.MUTED)
    def get(self):return self._value
    def configure(self,**kwargs):
        if 'state' in kwargs:
            self._state=kwargs.pop('state')
            for button in getattr(self,'_choices',{}).values():button.configure(state=self._state)
        if kwargs:super().configure(**kwargs)
    def cget(self,key):
        if key=='state':return self._state
        if key=='values':return list(self._choices)
        return super().cget(key)
