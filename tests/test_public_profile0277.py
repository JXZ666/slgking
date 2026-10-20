"""Public profile API/projection checks; no Tk root, real network, or user files."""
import io
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import slg_account
from slg_public_profile_ui import public_projection

PID = 'pp_' + 'a' * 32


class PublicProfileAPITests(unittest.TestCase):
    def test_valid_id_public_fetch_never_reads_session_or_adds_token(self):
        captured=[]
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*_):self.close()
        def open_request(req,*args,**kwargs):
            captured.append(req)
            return Response(json.dumps({'ok':True,'profile':{'public_id':PID,'nickname':'Guest','profile_visible':False}}).encode())
        with patch.object(slg_account,'session',side_effect=AssertionError('session read forbidden')), \
             patch.object(slg_account,'authenticated',side_effect=AssertionError('authenticated public request')), \
             patch.object(slg_account,'_open_with_proxy_fallback',side_effect=open_request):
            result=slg_account.public_profile(PID)
        self.assertEqual(result['profile']['public_id'],PID)
        self.assertEqual(len(captured),1)
        self.assertTrue(captured[0].full_url.endswith('/community/profile?public_id='+PID))
        self.assertEqual(captured[0].method,'GET')
        self.assertIsNone(captured[0].data)
        self.assertNotIn('authorization',{key.lower() for key in captured[0].headers})
        self.assertNotIn('cookie',{key.lower() for key in captured[0].headers})

    def test_public_id_validation_before_any_request(self):
        for value in (None,False,12,{},'', 'a'*32,'u_'+'a'*32,'pp_'+'a'*31,'pp_'+'a'*33,'pp_'+'A'*32,PID+'?token=secret',' '+PID):
            with self.subTest(value=value),patch.object(slg_account,'request',side_effect=AssertionError('invalid id sent')):
                with self.assertRaises(slg_account.AccountError):slg_account.public_profile(value)

    def test_visibility_update_preserves_boolean_and_uses_private_endpoint(self):
        for value in (True,False):
            with self.subTest(value=value),patch.object(slg_account,'authenticated',return_value={'ok':True,'profile_visible':value}) as call:
                self.assertEqual(slg_account.update_profile(profile_visible=value)['profile_visible'],value)
                call.assert_called_once_with('/account/profile',method='POST',payload={'profile_visible':value})

    def test_invalid_visibility_rejected_without_network(self):
        for value in ('false','true',0,1,[],{}):
            with self.subTest(value=value),patch.object(slg_account,'authenticated',side_effect=AssertionError('invalid flag sent')):
                with self.assertRaises(slg_account.AccountError):slg_account.update_profile(profile_visible=value)

    def test_legacy_profile_update_omits_flag(self):
        with patch.object(slg_account,'authenticated',return_value={'ok':True}) as call:
            slg_account.update_profile(nickname='  Alice  ',profile_message='Hello')
        call.assert_called_once_with('/account/profile',method='POST',payload={'nickname':'Alice','profile_message':'Hello'})
        with self.assertRaises(slg_account.AccountError):slg_account.update_profile()


class PublicProfileProjectionTests(unittest.TestCase):
    def test_hidden_strict_whitelist_drops_old_snapshots_and_private_avatar(self):
        value={'public_id':PID,'nickname':'Alice','profile_visible':False,
               'equipped_title':'secret-title','equipped_appearances':{'avatar_frame':'secret-frame'},
               'profile_message':'old message','avatar_path':'C:/private/avatar.png',
               'owned_titles':['inventory'],'balance':999,'account_id':'u_private','login_key':'secret'}
        result=public_projection({'ok':True,'profile':value},PID)
        self.assertEqual(result,{'public_id':PID,'nickname':'Alice','profile_visible':False})
        self.assertIn('equipped_title',value)

    def test_visible_projection_whitelist_and_lengths(self):
        value={'public_id':PID,'nickname':'A'*90,'profile_visible':True,'equipped_title':'title',
               'equipped_appearances':{'avatar_frame':'frame','comment_frame':'card','local_avatar':'C:/secret'},
               'profile_message':'M'*50,'owned_titles':['secret'],'email':'private'}
        result=public_projection({'profile':value},PID)
        self.assertEqual(set(result),{'public_id','nickname','profile_visible','title_id','appearances','profile_message'})
        self.assertEqual(result['nickname'],'A'*40)
        self.assertEqual(result['profile_message'],'M'*20)
        self.assertEqual(result['appearances'],{'avatar_frame':'frame','comment_frame':'card'})

    def test_malformed_response_never_defaults_public_or_uses_old_details(self):
        for response in (None,[],{}, {'profile':None},{'profile':{'public_id':'pp_'+'b'*32,'profile_visible':True}},
                         {'profile':{'public_id':PID}}, {'profile':{'public_id':PID,'profile_visible':'false'}},
                         {'profile':{'public_id':PID,'profile_visible':0}}):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):public_projection(response,PID)

    def test_empty_nickname_and_appearances_use_bounded_public_defaults(self):
        result=public_projection({'profile':{'public_id':PID,'profile_visible':True,'nickname':None,'equipped_appearances':None}},PID)
        self.assertEqual(result['nickname'],'匿名用户')
        self.assertEqual(result['appearances'],{})
        self.assertEqual(result['profile_message'],'')


if __name__=='__main__':unittest.main()
