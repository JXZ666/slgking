import queue
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import slg_account
import slg_comments
import slg_gui
from slg_notice_projection import notice_projection


class NoticeProjection0280(unittest.TestCase):
    def project(self,status=None,**kw):
        opts=dict(logged_in=True,identity_valid=True,test_build=False)
        opts.update(kw)
        status=status if status is not None else dict(campaign=dict(id='fixture',active=True,amount=100),claimed=False)
        return notice_projection(dict(announcement=dict(id='notice',title='公告')),status,**opts)
    def test_only_verified_eligible_reward_lights_dot(self):
        self.assertTrue(self.project()['claimable'])
        for kw in (dict(logged_in=False),dict(identity_valid=False),dict(developer=True),dict(test_build=True)):
            self.assertFalse(self.project(**kw)['claimable'])
    def test_unread_is_independent_from_reward(self):
        value=self.project(dict(campaign=None,claimed=False),unread=True)
        self.assertIn('新公告',value['announcements'][0]['title'])
        self.assertFalse(value['claimable'])
    def test_unknown_error_malformed_or_claimed_do_not_light_dot(self):
        for value in ({},{'error':'offline'},{'claimed':True,'campaign':{'id':'x','active':True,'amount':100}},
                      {'claimed':'false','campaign':{'id':'x','active':True,'amount':100}},
                      {'claimed':False,'campaign':{'id':'x','active':True,'amount':True}}):
            self.assertFalse(self.project(value)['claimable'])
    def test_explicit_test_channel_eligibility_is_strict(self):
        status=dict(campaign=dict(id='fixture',active=True,amount=100),claimed=False,claimable=True)
        self.assertTrue(self.project(status,test_build=True)['claimable'])
        status['claimable']='true'
        self.assertFalse(self.project(status,test_build=True)['claimable'])
    def test_delayed_comment_result_cannot_touch_another_identity(self):
        host=SimpleNamespace(_capture_identity_context=lambda:{'account_id':'B','generation':2},_comment_mine=['keep'])
        slg_gui.App._comments_result(host,'fixture',1,'latest',1,{'comments':[]},['private-A'],{'account_id':'A','generation':1})
        self.assertEqual(host._comment_mine,['keep'])
    def test_comment_worker_uses_captured_credentials(self):
        host=SimpleNamespace(queue=queue.Queue())
        snapshot=dict(account_id='A',token='test-only')
        seen=[]
        with patch.object(slg_comments,'fetch_comments_page',return_value={'comments':[]}),\
             patch.object(slg_comments,'fetch_my_comments',side_effect=lambda slug:seen.append(slg_account.session()['account_id']) or []):
            slg_gui.App._fetch_comments_worker(host,'fixture',1,'latest',1,{'account_id':'A'},snapshot)
        self.assertEqual(seen,['A'])
        self.assertEqual(host.queue.get()[1][-1],{'account_id':'A'})
    def test_detail_signature_tracks_binding_changes(self):
        game=dict(id=1,status=None,my_rating=None,cover_file=None,folder_path='old',folder_version='1',exe_path='a.exe')
        host=SimpleNamespace(selected=game,collection_id=None,origin='local')
        for key,value in (('folder_path','new'),('folder_version','2'),('exe_path','b.exe')):
            before=slg_gui.App._detail_signature(host);game[key]=value
            self.assertNotEqual(before,slg_gui.App._detail_signature(host))

if __name__=='__main__':unittest.main()
