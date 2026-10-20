"""Non-modal, entirely local avatar crop editor with owned worker lifecycle."""
from concurrent.futures import ThreadPoolExecutor
import io
import queue
import tkinter as tk
from tkinter import filedialog
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk
from slg_image_resources import ResourceToplevel
from slg_local_avatar import decode_source, encode_crop, save_avatar


def crop_box(size, zoom=1.0, center=None):
    """Square inside image, zoom 1..4. Center is in source pixels."""
    width, height = size
    side = min(width, height) / max(1.0, min(4.0, float(zoom)))
    cx, cy = center or (width/2, height/2)
    cx = max(side/2, min(width-side/2, cx))
    cy = max(side/2, min(height-side/2, cy))
    return (cx-side/2, cy-side/2, cx+side/2, cy+side/2)


def preview_crop(image, box):
    # Worker-only resize/decode. Tk image creation remains in _poll.
    image = image.resize((256,256), Image.Resampling.LANCZOS, box=box).convert("RGBA")
    mask = Image.new("L", (256,256)); ImageDraw.Draw(mask).ellipse((0,0,255,255), fill=255)
    image.putalpha(mask)
    return image


class AvatarEditor(ResourceToplevel):
    def __init__(self, owner, *, appdir, identity_key, is_current=lambda: True,
                 on_saved=None, executor=None, palette=None):
        self._owned_global = []
        super().__init__(owner)
        self.title("设置本机头像")
        self.geometry("520x610"); self.minsize(440,570)
        self.transient(owner)
        self._appdir, self._key = appdir, identity_key
        self._is_current, self._on_saved = is_current, on_saved
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="avatar")
        self._owns_executor = executor is None
        self._events = queue.Queue()
        self._closed = False; self._generation = 0; self._jobs = set(); self._futures = set()
        self._source = None; self._zoom = 1.; self._center = None; self._drag = None
        self._loading_source = False
        self._preview_future = None; self._render_job = None; self._photo = None; self._saving = False
        palette = palette or {}
        self._accent = palette.get("ACCENT", "#b32359")
        self._button_colors=dict(fg_color=self._accent,hover_color=palette.get("ACCENT_HOVER","#912047"),text_color="#ffffff",text_color_disabled=("#74646c","#c1b3bb"))
        self._surface = palette.get("PANEL", ("#f7f4f6", "#211a20"))
        self.configure(fg_color=self._surface)
        ctk.CTkLabel(self, text="本机头像", font=("Microsoft YaHei UI",20,"bold")).pack(pady=(18,4))
        ctk.CTkLabel(self, text="仅保存在本机，不上传、不随账号同步", wraplength=430).pack(pady=(0,8))
        self.canvas = tk.Canvas(self, width=276, height=276, highlightthickness=0, bg="#292229")
        self.canvas.pack(pady=8)
        self.canvas.create_text(138,138,text="选择图片后拖动头像调整区域",fill="white",width=230,tags="empty")
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", lambda _e: setattr(self,"_drag",None))
        controls = ctk.CTkFrame(self,fg_color="transparent"); controls.pack(fill="x",padx=28)
        ctk.CTkLabel(controls,text="缩放").pack(side="left",padx=(0,12))
        self.zoom_slider = ctk.CTkSlider(controls,from_=1,to=4,number_of_steps=90,command=self._set_zoom,progress_color=self._accent,button_color=self._accent,button_hover_color=palette.get("ACCENT_HOVER","#912047"))
        self.zoom_slider.set(1); self.zoom_slider.pack(side="left",fill="x",expand=True)
        self.reset_button = ctk.CTkButton(controls,text="重置",width=62,command=self.reset_crop,**self._button_colors)
        self.reset_button.pack(side="right",padx=(12,0))
        self.message = ctk.CTkLabel(self,text="支持JPEG、PNG、WebP静态图片；拖动圆形预览调整位置",wraplength=450)
        self.message.pack(padx=20,pady=12)
        row = ctk.CTkFrame(self,fg_color="transparent"); row.pack(pady=6)
        ctk.CTkButton(row,text="选择图片…",width=120,command=self.choose,**self._button_colors).pack(side="left",padx=6)
        self.save_button = ctk.CTkButton(row,text="保存头像",width=120,state="disabled",command=self.save,**self._button_colors)
        self.save_button.pack(side="left",padx=6)
        ctk.CTkButton(self,text="取消",width=120,command=self.destroy,**self._button_colors).pack(pady=10)
        self.bind("<Escape>",lambda _e:self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._later(30,self._poll)

    def bind_all(self, sequence=None, func=None, add=None):
        fid=super().bind_all(sequence,func,add)
        if func is not None and fid:self._owned_global.append((sequence,fid))
        return fid

    def _later(self, ms, callback):
        holder=[]
        def run():
            if holder:self._jobs.discard(holder[0])
            if not self._closed:callback()
        job=self.after(ms,run); holder.append(job); self._jobs.add(job)
        return job

    def _valid(self):
        return not self._closed and bool(self._is_current())

    def _submit(self, kind, generation, function, *args):
        try:
            future=self._executor.submit(function,*args)
        except Exception as exc:
            self._events.put((kind,generation,None,(False,exc)))
            return None
        self._futures.add(future)
        def done(result):
            # No widget methods or file writes in completion callbacks.
            try: payload=(True,result.result())
            except Exception as exc:payload=(False,exc)
            if not self._closed:self._events.put((kind,generation,result,payload))
        future.add_done_callback(done)
        return future

    def choose(self):
        if self._saving:return
        path=filedialog.askopenfilename(parent=self,title="选择本机头像",filetypes=[("图片","*.jpg *.jpeg *.png *.webp"),("所有文件","*.*")])
        if path:self.load_path(path)

    def load_path(self,path):
        if not self._valid() or self._saving:return
        self._generation+=1
        self._loading_source=True
        self._drag=None
        self.zoom_slider.configure(state="disabled")
        self.reset_button.configure(state="disabled")
        if self._render_job:
            self.after_cancel(self._render_job); self._jobs.discard(self._render_job)
            self._render_job=None
        self.save_button.configure(state="disabled")
        self.message.configure(text="正在读取图片…")
        for future in tuple(self._futures):future.cancel()
        self._submit("source",self._generation,decode_source,path)

    def _set_zoom(self,value):
        if self._saving or self._loading_source:return
        self._zoom=float(value); self._request_preview()

    def reset_crop(self):
        if self._saving or self._loading_source:return
        self._zoom=1.; self._center=None; self.zoom_slider.set(1); self._request_preview()

    def _drag_start(self,event):
        if self._source is None or self._saving or self._loading_source:return
        box=crop_box(self._source.size,self._zoom,self._center)
        self._center=((box[0]+box[2])/2,(box[1]+box[3])/2)
        self._drag=(event.x,event.y,self._center)

    def _drag_move(self,event):
        if self._drag is None or self._source is None or self._saving or self._loading_source:return
        x,y,(cx,cy)=self._drag
        box=crop_box(self._source.size,self._zoom,self._center)
        scale=(box[2]-box[0])/256
        self._center=(cx-(event.x-x)*scale,cy-(event.y-y)*scale)
        self._request_preview()

    def _request_preview(self):
        if self._source is None or self._loading_source or not self._valid():return
        self._generation+=1
        if self._preview_future is not None:self._preview_future.cancel()
        if self._render_job:
            self.after_cancel(self._render_job); self._jobs.discard(self._render_job)
        self._render_job=self._later(40,self._render_preview)

    def _render_preview(self):
        self._render_job=None
        if self._loading_source or not self._valid():return
        self._preview_future=self._submit("preview",self._generation,preview_crop,self._source,
                                         crop_box(self._source.size,self._zoom,self._center))

    def save(self):
        if self._source is None or self._loading_source or not self._valid() or self._saving:return
        self._saving=True; self._generation+=1
        self.save_button.configure(state="disabled"); self.message.configure(text="正在压缩头像…")
        self._submit("encoded",self._generation,encode_crop,self._source,
                     crop_box(self._source.size,self._zoom,self._center))

    def _poll(self):
        if not self._valid():self.destroy(); return
        while not self._events.empty():
            kind,generation,future,(ok,value)=self._events.get_nowait(); self._futures.discard(future)
            if generation!=self._generation:continue
            if not ok:
                if kind=="source":
                    self._loading_source=False
                    self.zoom_slider.configure(state="normal")
                    self.reset_button.configure(state="normal")
                self._saving=False; self.message.configure(text=str(value))
                self.save_button.configure(state="normal" if self._source is not None else "disabled")
                continue
            if kind=="source":
                self._loading_source=False
                self.zoom_slider.configure(state="normal")
                self.reset_button.configure(state="normal")
                self._source=value; self._zoom=1.; self._center=None; self.zoom_slider.set(1)
                self.save_button.configure(state="normal"); self.message.configure(text="拖动头像、缩放选区；保存前可随时取消")
                self._request_preview()
            elif kind=="preview":
                self._photo=ImageTk.PhotoImage(value,master=self)
                self.canvas.delete("all"); self.canvas.create_image(138,138,image=self._photo)
            elif kind=="encoded":
                # The only write point is guarded on the owner Tk thread.
                try:
                    if not self._valid():continue
                    path=save_avatar(self._appdir,self._key,value)
                except Exception as exc:
                    self._saving=False; self.save_button.configure(state="normal"); self.message.configure(text="保存失败，旧头像已保留："+str(exc)); continue
                callback=self._on_saved
                self.destroy()
                if callback:callback(path)
                return
        self._later(30,self._poll)

    def destroy(self):
        if getattr(self,"_closed",True):return
        self._closed=True; self._generation+=1
        for job in tuple(self._jobs):
            try:self.after_cancel(job)
            except tk.TclError:pass
        self._jobs.clear()
        for future in tuple(self._futures):future.cancel()
        self._futures.clear()
        if self._owns_executor:self._executor.shutdown(wait=False,cancel_futures=True)
        self._source=None; self._photo=None
        root=self._root()
        for sequence,fid in self._owned_global:
            try:
                root._unbind(('bind','all',sequence),fid)
                script=root.tk.call('bind','all',sequence)
                lines=[line for line in str(script).splitlines() if line.strip()]
                root.tk.call('bind','all',sequence,'\n'.join(lines)+('\n' if lines else ''))
            except tk.TclError:pass
        self._owned_global.clear()
        while not self._events.empty():self._events.get_nowait()
        super().destroy()
