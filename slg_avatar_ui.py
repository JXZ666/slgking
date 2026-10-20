"""Main-window adapter for private avatars; the account hover reads memory only."""
import tkinter as tk
import customtkinter as ctk
from PIL import Image, ImageDraw
import slg_db
from slg_local_avatar import identity_key, load_avatar, reset_avatar
from slg_avatar_editor import AvatarEditor
from slg_image_resources import ImageLRU


class LocalAvatarUI:
    def _current_local_avatar_key(self):
        context=self._capture_identity_context()
        return identity_key(context.get("account_id"),context.get("admin",False))

    def _avatar_context_valid(self, context, generation):
        closing=getattr(self,"_closing_event",None)
        return (not (closing is not None and closing.is_set())
                and context==self._capture_identity_context()
                and generation==getattr(self,"_local_avatar_generation",0))

    def _reload_local_avatar(self, force=False):
        context=self._capture_identity_context()
        key=identity_key(context.get("account_id"),context.get("admin",False))
        if not force and key==getattr(self,"_local_avatar_requested_key",None):return
        same_identity=(key==getattr(self,"_local_avatar_requested_key",None))
        self._local_avatar_requested_key=key
        self._local_avatar_generation=getattr(self,"_local_avatar_generation",0)+1
        generation=self._local_avatar_generation
        self._local_avatar_loading=True
        if not same_identity:
            self._local_avatar_loaded_key=None
            self._local_avatar_pil=None
            self._local_avatar_images=ImageLRU(max_entries=8,max_bytes=4*1024*1024)
        # Identity changes must remove the old picture before any asynchronous read.
        self._refresh_own_avatar_views()
        appdir=slg_db.app_dir()
        def complete(value,error):
            if not self._avatar_context_valid(context,generation):return
            self._local_avatar_loading=False
            if error is None:
                self._local_avatar_loaded_key=key
                self._local_avatar_pil=value
                self._local_avatar_images=ImageLRU(max_entries=8,max_bytes=4*1024*1024)
            if error is not None:
                # A missing/corrupt private file never breaks account rendering.
                self._set_progress("本机头像读取失败，可在个人中心重新选择："+str(error))
            self._refresh_own_avatar_views()
        def worker():
            try:
                image=load_avatar(appdir,key)
                if image is not None:
                    image=image.convert("RGBA")
                    mask=Image.new("L",image.size)
                    ImageDraw.Draw(mask).ellipse((0,0,image.width-1,image.height-1),fill=255)
                    image.putalpha(mask)
                error=None
            except Exception as exc:image,error=None,exc
            self.queue.put(("library_callback",(complete,image,error)))
        try:self._cover_executor.submit(worker)
        except Exception as exc:complete(None,exc)

    def _current_local_avatar_pil(self):
        if getattr(self,"_local_avatar_loaded_key",None)!=getattr(self,"_local_avatar_requested_key",None):return None
        return getattr(self,"_local_avatar_pil",None)

    def _own_avatar_image(self,size=48):
        """Pure memory/Tk projection. Never read a path, session, or network here."""
        if (getattr(self,"_local_avatar_loaded_key",None)!=getattr(self,"_local_avatar_requested_key",None)
                or getattr(self,"_local_avatar_pil",None) is None):return None
        size=max(16,min(256,int(size)))
        cache=self._local_avatar_images
        image=cache.get(size)
        if image is None:
            image=ctk.CTkImage(light_image=self._local_avatar_pil,dark_image=self._local_avatar_pil,size=(size,size))
            cache[size]=image
        return image

    def _refresh_own_avatar_views(self):
        refresh=getattr(self,"_schedule_account_chip_refresh",None)
        if refresh:refresh()
        callback=getattr(self,"_refresh_local_avatar_profile",None)
        if callback:callback()
        else:
            label=getattr(self,"_own_avatar_profile_label",None)
            if label is not None:
                try:
                    if label.winfo_exists():
                        picture=self._own_avatar_image(48)
                        label.configure(image=picture,text="" if picture is not None else getattr(label,"_avatar_initial","游"))
                except tk.TclError:pass

    def open_local_avatar_editor(self):
        editor=getattr(self,"_local_avatar_editor",None)
        if editor is not None:
            try:
                if editor.winfo_exists():editor.lift(); editor.focus_set(); return
            except tk.TclError:pass
        context=self._capture_identity_context()
        key=identity_key(context.get("account_id"),context.get("admin",False))
        if key!=getattr(self,"_local_avatar_requested_key",None):self._reload_local_avatar()
        def current():
            closing=getattr(self,"_closing_event",None)
            return (not (closing is not None and closing.is_set())
                    and context.get("generation")==getattr(self,"_identity_generation",0)
                    and key==getattr(self,"_local_avatar_requested_key",None))
        def saved(_path):
            self._local_avatar_editor=None
            if current():
                self._reload_local_avatar(force=True)
                self._set_progress("本机头像已保存，不上传、不随账号同步")
        self._local_avatar_editor=AvatarEditor(self,appdir=slg_db.app_dir(),identity_key=key,
            is_current=current,on_saved=saved,executor=self._cover_executor)
        return self._local_avatar_editor

    def reset_local_avatar(self):
        context=self._capture_identity_context()
        key=identity_key(context.get("account_id"),context.get("admin",False))
        self._close_local_avatar_editor()
        try:reset_avatar(slg_db.app_dir(),key)
        except OSError as exc:
            self._set_progress("恢复昵称首字失败，旧头像已保留："+str(exc)); return False
        self._reload_local_avatar(force=True)
        self._set_progress("已恢复昵称首字；本机头像已移除")
        return True

    def _close_local_avatar_editor(self):
        editor=getattr(self,"_local_avatar_editor",None)
        self._local_avatar_editor=None
        if editor is not None:
            try:editor.destroy()
            except tk.TclError:pass

    def _dispose_local_avatar(self):
        self._close_local_avatar_editor()
        self._local_avatar_generation=getattr(self,"_local_avatar_generation",0)+1
        self._local_avatar_loading=False
        self._local_avatar_requested_key=None; self._local_avatar_loaded_key=None; self._local_avatar_pil=None
        cache=getattr(self,"_local_avatar_images",None)
        if cache is not None:cache.clear()
