"""Accessible, themed action popup. UI calls only; never holds a modal grab."""
import tkinter as tk
import customtkinter as ctk
from slg_image_resources import ResourceToplevel, ResourceButton, ResourceScrollableFrame


def menu_geometry(anchor, bounds, width, height, gap=6):
    """Return physical x/y/width/height constrained to the current monitor."""
    ax, ay, aw, ah = anchor
    left, top, right, bottom = bounds
    width=min(width,max(1,right-left-16)); height=min(height,max(1,bottom-top-16))
    x=max(left+8,min(ax,right-width-8))
    y=ay+ah+gap
    if y+height>bottom-8: y=ay-height-gap
    y=max(top+8,min(y,bottom-height-8))
    return round(x),round(y),round(width),round(height)


def _remove_binding(root, sequence, fid):
    root._unbind(('bind','all',sequence),fid)
    # Tkinter's exact unbind leaves a blank line at each removal. Compact only
    # empty script lines so hundreds of menu openings do not grow root script.
    script=root.tk.call('bind','all',sequence)
    lines=[line for line in str(script).splitlines() if line.strip()]
    root.tk.call('bind','all',sequence,'\n'.join(lines)+('\n' if lines else ''))


class _MenuPopup(ResourceToplevel):
    # CTkToplevel registers a root-level click-to-focus command in __init__.
    # Own that exact registration too; destroying a normal CTk popup otherwise
    # retains a closure referring to every closed popup.
    def __init__(self, *args, **kwargs):
        self._owned_global_bindings=[]
        super().__init__(*args, **kwargs)

    def bind_all(self, sequence=None, func=None, add=None):
        fid=super().bind_all(sequence,func,add)
        if func is not None and fid:self._owned_global_bindings.append((sequence,fid))
        return fid

    def destroy(self):
        for sequence,fid in self._owned_global_bindings:
            try:_remove_binding(self._root(),sequence,fid)
            except tk.TclError:pass
        self._owned_global_bindings.clear()
        super().destroy()


class ActionMenu:
    """Reusable controller. actions=(label, callback[, enabled bool/callable]).

    Construct with CTkFont from ui_font(size=14), palette dict(card,hover,text,
    muted,accent), optional screen_bounds(widget)->physical monitor rectangle.
    open(anchor, actions) closes the previous menu on the same Tk root. close()
    releases all popup bindings/resources; dispose() permanently releases owner.
    """
    def __init__(self, master, *, font, palette, screen_bounds=None, row_height=44, min_width=240):
        self.master=master; self.root=master._root(); self.font=font
        self.colors=palette; self.screen_bounds=screen_bounds
        self.row_height=max(44,row_height); self.min_width=max(240,min_width)
        self.popup=None; self.anchor=None; self.actions=[]; self.buttons=[]
        self.scroller=None; self.selected=None; self._bindings=[]
        self._anchor_binding=None; self._disposed=False

    def _enabled(self, index):
        value=self.actions[index][2] if len(self.actions[index])>2 else True
        return bool(value() if callable(value) else value)

    def open(self, anchor, actions):
        if self._disposed: return
        self.close(return_focus=False)
        old=getattr(self.root,'_slg_action_menu',None)
        if old is not None and old is not self:old.close(return_focus=False)
        self.root._slg_action_menu=self
        self.anchor=anchor; self.actions=list(actions)
        if not self.actions:
            self.close(return_focus=False); return
        p=self.popup=_MenuPopup(self.master)
        p.withdraw(); p.overrideredirect(True); p.transient(anchor.winfo_toplevel())
        p.configure(fg_color=self.colors['card'])
        ws=ctk.ScalingTracker.get_widget_scaling(anchor)
        ss=ctk.ScalingTracker.get_window_scaling(p)
        bounds=(p.winfo_vrootx(),p.winfo_vrooty(),p.winfo_vrootx()+p.winfo_vrootwidth(),p.winfo_vrooty()+p.winfo_vrootheight())
        if self.screen_bounds:bounds=self.screen_bounds(p)
        font_height=max(14,abs(self.font.cget('size')))
        line=max(self.row_height,font_height+22)
        width=max(self.min_width*ws,max(self.font.measure(str(a[0])) for a in self.actions)+48*ws)
        desired=(line*len(self.actions)+16)*ws
        x,y,pw,ph=menu_geometry((anchor.winfo_rootx(),anchor.winfo_rooty(),anchor.winfo_width(),anchor.winfo_height()),bounds,width,desired,6*ws)
        needs_scroll=desired>ph
        if needs_scroll:
            self.scroller=ResourceScrollableFrame(p,fg_color='transparent',corner_radius=0)
            body=self.scroller
        else:body=ctk.CTkFrame(p,fg_color='transparent',corner_radius=0)
        body.pack(fill='both',expand=True,padx=4,pady=4)
        self.buttons=[]
        for i,action in enumerate(self.actions):
            b=ResourceButton(body,text=str(action[0]),font=self.font,height=line,
                anchor='w',corner_radius=6,fg_color=self.colors['card'],
                hover_color=self.colors['hover'],text_color=self.colors['text'],
                text_color_disabled=self.colors['muted'],state='normal' if self._enabled(i) else 'disabled',
                command=lambda idx=i:self.invoke(idx))
            b.pack(fill='x',padx=2,pady=0)
            b.bind('<Enter>',lambda _,idx=i:self._select(idx),add='+')
            self.buttons.append(b)
        p.geometry(f'{max(1,round(pw/ss))}x{max(1,round(ph/ss))}{x:+d}{y:+d}')
        p.bind('<Destroy>',self._destroyed,add='+')
        self._anchor_surface=getattr(anchor,'_canvas',anchor)
        self._anchor_binding=self._anchor_surface.bind('<Destroy>',self._anchor_destroyed,add='+')
        for sequence,callback in (('<ButtonPress-1>',self._outside),('<ButtonPress-3>',self._outside),('<KeyPress>',self._keyboard)):
            fid=self.root.bind_all(sequence,callback,add='+'); self._bindings.append((sequence,fid))
        self.selected=None; self._key('Home')
        p.deiconify(); p.lift(); p.focus_set()

    def _select(self, index):
        if self.popup is None or not self._enabled(index):return
        self.selected=index
        for i,b in enumerate(self.buttons):
            enabled=self._enabled(i)
            b.configure(state='normal' if enabled else 'disabled',
                fg_color=self.colors['hover'] if i==index else self.colors['card'],
                border_width=1 if i==index else 0,border_color=self.colors['accent'])
        if self.scroller is not None:
            canvas=self.scroller._parent_canvas
            total=max(1,len(self.buttons)*self.buttons[index].winfo_reqheight())
            top=index*self.buttons[index].winfo_reqheight()
            first,last=canvas.yview()
            if top<first*total:canvas.yview_moveto(top/total)
            elif top+self.buttons[index].winfo_reqheight()>last*total:
                canvas.yview_moveto(max(0,(top+self.buttons[index].winfo_reqheight()-canvas.winfo_height())/total))

    def _keyboard(self,event):
        # Only intercept menu navigation keys while this popup exists.
        if self.popup is None:return
        if event.keysym in ('Up','Down','Home','End','Return','KP_Enter','space','Escape','Tab'):
            return self._key(event.keysym)

    def _key(self,key):
        if self.popup is None:return
        if key in ('Escape','Tab'):self.close(); return 'break'
        if key in ('Return','KP_Enter','space'):
            if self.selected is not None:self.invoke(self.selected)
            return 'break'
        enabled=[i for i in range(len(self.actions)) if self._enabled(i)]
        if not enabled:return 'break'
        if key=='Home':target=enabled[0]
        elif key=='End':target=enabled[-1]
        else:
            if self.selected in enabled:
                pos=enabled.index(self.selected); target=enabled[(pos+(-1 if key=='Up' else 1))%len(enabled)]
            else:target=enabled[-1] if key=='Up' else enabled[0]
        self._select(target); return 'break'

    def invoke(self,index):
        if self.popup is None or not self._enabled(index):return
        command=self.actions[index][1]
        self.close()
        command()

    def _outside(self,event):
        widget=event.widget
        while widget is not None:
            if widget is self.popup or widget is self.anchor:return
            widget=getattr(widget,'master',None)
        self.close(return_focus=False)

    def _anchor_destroyed(self,event):
        if event.widget is getattr(self,'_anchor_surface',None):self.close(return_focus=False)

    def _destroyed(self,event):
        if event.widget is self.popup:self.close(return_focus=False)

    def close(self,return_focus=True):
        p,self.popup=self.popup,None
        anchor,self.anchor=self.anchor,None
        for sequence,fid in self._bindings:
            try:_remove_binding(self.root,sequence,fid)
            except tk.TclError:pass
        self._bindings.clear()
        if self._anchor_binding and anchor is not None:
            try:self._anchor_surface.unbind('<Destroy>',self._anchor_binding)
            except tk.TclError:pass
        self._anchor_binding=None
        if getattr(self.root,'_slg_action_menu',None) is self:self.root._slg_action_menu=None
        self.buttons=[]; self.scroller=None; self.selected=None; self.actions=[]
        if p is not None:
            try:p.destroy()
            except tk.TclError:pass
        if return_focus and anchor is not None:
            try:
                if anchor.winfo_exists():getattr(anchor,'_canvas',anchor).focus_set()
            except tk.TclError:pass

    destroy=close

    def dispose(self):
        self.close(return_focus=False); self._disposed=True
