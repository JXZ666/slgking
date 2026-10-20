import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
import slg_local_avatar as avatar
from slg_avatar_editor import crop_box, preview_crop

class LocalAvatarTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"中文资料"
        self.key=avatar.identity_key("alice")
        self.image=Image.new("RGB",(600,400),"red")
        self.data=avatar.encode_crop(self.image,(100,0,500,400))
    def test_identities_are_safe_distinct(self):
        values={avatar.identity_key(),avatar.identity_key("alice"),avatar.identity_key("alice",True),avatar.identity_key("bob"),avatar.identity_key("../../x")}
        self.assertEqual(len(values),5)
        for key in values:self.assertEqual(avatar.avatar_path(self.root,key).parent,self.root/"avatars")
        with self.assertRaises(ValueError):avatar.avatar_path(self.root,"../x")
    def test_save_restart_reset_isolated(self):
        path=avatar.save_avatar(self.root,self.key,self.data)
        self.assertTrue(path.exists()); self.assertEqual(avatar.load_avatar(self.root,self.key).size,(256,256))
        self.assertIsNone(avatar.load_avatar(self.root,avatar.identity_key("bob")))
        avatar.reset_avatar(self.root,self.key); self.assertIsNone(avatar.load_avatar(self.root,self.key))
        avatar.reset_avatar(self.root,self.key)
    def test_failed_replace_preserves_old_and_cleans_temp(self):
        path=avatar.save_avatar(self.root,self.key,self.data); before=path.read_bytes()
        other=avatar.encode_crop(Image.new("RGB",(400,400),"blue"),(0,0,400,400))
        with patch.object(avatar.os,"replace",side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):avatar.save_avatar(self.root,self.key,other)
        self.assertEqual(path.read_bytes(),before); self.assertEqual(list(path.parent.glob("*.tmp")),[])
    def test_hard_bounds_and_no_metadata(self):
        noisy=Image.effect_noise((1024,1024),100).convert("RGB")
        data=avatar.encode_crop(noisy,(0,0,1024,1024))
        self.assertLessEqual(len(data),65536)
        with Image.open(io.BytesIO(data)) as image:
            self.assertLessEqual(image.width,256); self.assertFalse(image.info)
    def test_decode_real_format_orientation_and_metadata(self):
        self.root.mkdir(); path=self.root/"misnamed.bin"
        exif=Image.Exif(); exif[274]=6; exif[315]="private author"
        Image.new("RGB",(200,100),"green").save(path,format="JPEG",exif=exif)
        image=avatar.decode_source(path)
        self.assertEqual(image.size,(100,200)); self.assertFalse(image.info)
    def test_animation_corruption_and_limits(self):
        self.root.mkdir(); path=self.root/"bad.png"; path.write_bytes(b"broken")
        with self.assertRaises(ValueError):avatar.decode_source(path)
        Image.new("RGB",(10,10)).save(path,save_all=True,append_images=[Image.new("RGB",(10,10),"red")])
        with self.assertRaises(ValueError):avatar.decode_source(path)
        Image.new("RGB",(100,100)).save(path)
        with patch.object(avatar,"MAX_SOURCE_PIXELS",999):
            with self.assertRaises(ValueError):avatar.decode_source(path)
        with patch.object(avatar,"MAX_SOURCE_BYTES",3):
            with self.assertRaises(ValueError):avatar.decode_source(path)
    def test_crop_invalid_and_bounds(self):
        for box in ((0,0,10,20),(-1,0,399,400),(0,0,900,900),(0,0,float("nan"),10)):
            with self.assertRaises(ValueError):avatar.encode_crop(self.image,box)
        for center in ((-999,-999),(9999,9999),None):
            box=crop_box((600,400),4,center)
            self.assertGreaterEqual(box[0],0); self.assertGreaterEqual(box[1],0)
            self.assertLessEqual(box[2],600); self.assertLessEqual(box[3],400)
        preview=preview_crop(self.image,(100,0,500,400))
        self.assertEqual(preview.getpixel((0,0))[3],0); self.assertEqual(preview.getpixel((128,128))[3],255)
    def test_bad_output_never_replaces_old(self):
        path=avatar.save_avatar(self.root,self.key,self.data)
        with self.assertRaises(ValueError):avatar.save_avatar(self.root,self.key,b"bad")
        self.assertEqual(path.read_bytes(),self.data)

class LocalAvatarAdapterTests(unittest.TestCase):
    def setUp(self):
        from slg_avatar_ui import LocalAvatarUI
        import queue
        import threading
        class Executor:
            def __init__(self):self.tasks=[]
            def submit(self,task):self.tasks.append(task)
        class Fake(LocalAvatarUI):
            def __init__(self):
                self.context=dict(account_id="a",admin=False,generation=0)
                self.queue=queue.Queue();self._cover_executor=Executor()
                self._closing_event=threading.Event();self.refreshes=0;self.errors=[]
            def _capture_identity_context(self):return self.context.copy()
            def _schedule_account_chip_refresh(self):self.refreshes+=1
            def _set_progress(self,text):self.errors.append(text)
        self.app=Fake()
    def test_late_identity_read_never_applies(self):
        import slg_avatar_ui as ui
        with patch.object(ui,"load_avatar",return_value=Image.new("RGB",(128,128),"red")):
            self.app._reload_local_avatar();self.app._cover_executor.tasks.pop()()
            self.app.context=dict(account_id="b",admin=False,generation=1)
            _kind,(callback,value,error)=self.app.queue.get_nowait();callback(value,error)
        self.assertIsNone(self.app._current_local_avatar_pil())
    def test_repeat_load_skips_and_projection_has_no_io(self):
        import slg_avatar_ui as ui
        with patch.object(ui,"load_avatar",return_value=None) as loader:
            self.app._reload_local_avatar();self.app._reload_local_avatar()
            self.assertEqual(len(self.app._cover_executor.tasks),1)
            self.app._cover_executor.tasks.pop()()
            _kind,(callback,value,error)=self.app.queue.get_nowait();callback(value,error)
            with patch.object(self.app,"_capture_identity_context",side_effect=AssertionError("session read")):
                self.assertIsNone(self.app._own_avatar_image())
                self.assertIsNone(self.app._current_local_avatar_pil())
            self.assertEqual(loader.call_count,1)
    def test_same_identity_retry_keeps_previous_picture_on_read_failure(self):
        import slg_avatar_ui as ui
        with patch.object(ui,"load_avatar",return_value=Image.new("RGB",(128,128),"red")):
            self.app._reload_local_avatar();self.app._cover_executor.tasks.pop()()
            _kind,(callback,value,error)=self.app.queue.get_nowait();callback(value,error)
        previous=self.app._current_local_avatar_pil()
        self.app._reload_local_avatar(force=True)
        self.assertIs(self.app._current_local_avatar_pil(),previous)
        self.assertTrue(self.app._local_avatar_loading)
        with patch.object(ui,"load_avatar",side_effect=PermissionError("denied")):
            self.app._cover_executor.tasks.pop()()
            _kind,(callback,value,error)=self.app.queue.get_nowait();callback(value,error)
        self.assertIs(self.app._current_local_avatar_pil(),previous)
        self.assertFalse(self.app._local_avatar_loading)

    def test_dispose_invalidates_pending(self):
        import slg_avatar_ui as ui
        with patch.object(ui,"load_avatar",return_value=Image.new("RGB",(128,128),"red")):
            self.app._reload_local_avatar();self.app._cover_executor.tasks.pop()()
            self.app._dispose_local_avatar()
            _kind,(callback,value,error)=self.app.queue.get_nowait();callback(value,error)
        self.assertIsNone(self.app._current_local_avatar_pil())

if __name__=="__main__":unittest.main()
