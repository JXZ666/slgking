import hashlib
import io
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image
import slg_dlsite_online
import slg_gui


class ImmediateExecutor:
    def __init__(self): self.calls = 0
    def submit(self, func):
        self.calls += 1
        func()


class CoverBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        raw = io.BytesIO()
        Image.new('RGB',(20,30),'blue').save(raw,'JPEG')
        self.raw = raw.getvalue()
        digest = hashlib.sha256(self.raw).hexdigest()
        self.name = 'dlsite-RJ123456-'+digest+'.jpg'
        item = {'filename':self.name,'sha256':digest,'bytes':len(self.raw)}
        self.fetches=[]
        def fetch(path):
            self.fetches.append(path)
            return self.raw
        self.app = SimpleNamespace(_network_enabled=True, _dlsite_cover_index={'dlsite-rj123456':item},
            _cover_waiters={}, _card_render_generation=1, queue=queue.Queue(),
            _download_executor=ImmediateExecutor(), _cover_executor=ImmediateExecutor(),
            _dlsite_cover_cache=slg_dlsite_online.CoverCache(self.temp.name,fetch))
        class Widget:
            def configure(self, **kwargs): pass
        self.widget = Widget()
        self.game={'slug':'dlsite-rj123456','cover_file':self.name,'has_cover_override':False}
        self.addCleanup(slg_gui._image_cache.clear)

    def bind(self):
        with patch.object(slg_gui.slg_db,'covers_dir',return_value=self.temp.name), \
             patch.object(slg_gui,'load_cover',return_value=None):
            slg_gui.App._bind_cover(self.app,self.widget,self.game)

    def test_existing_corrupt_cache_is_redownloaded_and_decoded(self):
        target=Path(self.temp.name)/self.name
        target.write_bytes(b'broken')
        self.bind()
        self.assertEqual(self.fetches,['/covers/'+self.name])
        self.assertEqual(target.read_bytes(),self.raw)
        self.assertIsNotNone(self.app.queue.get()[1][1])

    def test_valid_cache_is_checked_without_network(self):
        (Path(self.temp.name)/self.name).write_bytes(self.raw)
        self.bind()
        self.assertEqual(self.fetches,[])
        self.assertIsNotNone(self.app.queue.get()[1][1])

    def test_personal_override_is_not_replaced(self):
        (Path(self.temp.name)/self.name).write_bytes(self.raw)
        self.game['has_cover_override']=True
        self.bind()
        self.assertEqual(self.fetches,[])
        self.assertEqual(self.app._download_executor.calls,0)

    def test_not_published_cover_does_not_request_upstream(self):
        self.game['cover_file']=None
        self.bind()
        self.assertEqual(self.fetches,[])
        self.assertTrue(self.app.queue.empty())


if __name__=='__main__':unittest.main()
