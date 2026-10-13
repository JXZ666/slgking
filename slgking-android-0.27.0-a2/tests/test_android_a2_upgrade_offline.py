"""Real historical-core -> a2 private-storage compatibility; no device or paid API.

Frozen Git commits generate isolated data. Tests do not downgrade an emulator.
Legacy 0.26 URLs are injected through its DB core (no URL editor existed then).
"""
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = [
    ('0.26.0-a1/code8', '40dfc7777ac06da4afca7aa5a4a1f45cfc684daf'),
    ('0.27.0-a1/code9', '75b7ac0'),
]

SEED = r"""
import json, socket, sys
from pathlib import Path
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden'))
import slg_android_bootstrap as boot
boot.init(sys.argv[1], Path.cwd() / 'assets', crypto='stub')
import slg_android_api as api
import slg_db as db

def call(method, **params):
    result = json.loads(api.call(json.dumps(dict(params, method=method))))
    assert result['ok'], (method, result)
    return result

gid = call('usergames.add', title='Offline upgrade marker', version='v1',
           categories=['slg', 'rpg'], tags=['upgrade-test'])['id']
call('game.rate', id=gid, stars=4)
call('game.note', id=gid, note='Retain private remark')
call('game.state', id=gid, status='want')
cid = call('collection.save', name='Retain collection')['id']
call('game.collections.set', id=gid, collection_ids=[cid])
# 0.26 had no native URL editor; its existing DB still supports imported URLs.
api._conn().execute('UPDATE games SET url=? WHERE id=?', ('https://example.invalid/personal',gid))
api._conn().commit()
call('translate.save', provider='custom', engine='openai',
     base_url='https://example.invalid/v1', model='legacy-custom-model', api_key='fixture-only-no-real-key')
prefs = dict(api._conn().execute("SELECT key,value FROM prefs WHERE key LIKE 'translate_%'"))
backup = call('backup.export')['data']
assert 'fixture-only-no-real-key' not in json.dumps(backup)
Path(sys.argv[2]).write_text(json.dumps({'id':gid, 'collection_id':cid,
    'prefs':prefs, 'backup':backup}), encoding='utf-8')
api._close_conn()
"""

VERIFY = r"""
import json, socket, sys
from pathlib import Path
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden'))
import slg_android_bootstrap as boot
import slg_android_api as api
import slg_db as db
root = Path.cwd()
expected = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
boot.init(sys.argv[1], root / 'assets', crypto='stub')

def call(method, **params):
    result = json.loads(api.call(json.dumps(dict(params, method=method))))
    assert result['ok'], (method,result)
    return result

def verify_personal():
    game=call('game.get', id=expected['id'])['game']
    assert (game['title'],game['my_rating'],game['note'],game['status'],game['url']) == (
        'Offline upgrade marker',4,'Retain private remark','want','https://example.invalid/personal')
    assert set(game['categories']) == {'slg','rpg'}
    rows=call('catalog.list',view='collection',collection_id=expected['collection_id'],catalog_id='all')
    assert any(g['id']==expected['id'] for g in rows['items'])
    assert any(c['name']=='Retain collection' for c in call('collections.list')['collections'])
    assert dict(api._conn().execute("SELECT key,value FROM prefs WHERE key LIKE 'translate_%'")) == expected['prefs']

verify_personal()
settings=call('translate.settings')
assert settings['provider']=='custom' and settings['model']=='legacy-custom-model'
assert settings['base_url']=='https://example.invalid/v1' and settings['has_key']
assert 'api_key' not in settings and 'fixture-only-no-real-key' not in json.dumps(settings)
verify_personal()  # Read did not migrate stored values.
call('translate.save',provider='custom',engine='openai',base_url=settings['base_url'],
     model=settings['model'],api_key='')
verify_personal()  # Same-target blank key keeps the existing stored value.
# Preview is zero-write; import restores user state and deliberately excludes credentials.
backup_path=Path(sys.argv[1])/'legacy-backup.json'
backup_path.write_text(json.dumps({'data':expected['backup']}),encoding='utf-8')
call('backup.preview',path=str(backup_path))
verify_personal()
call('game.note',id=expected['id'],note='Changed only in fixture')
call('backup.import',path=str(backup_path))
verify_personal()
exported=call('backup.export')['data']
assert 'fixture-only-no-real-key' not in json.dumps(exported)
api._close_conn()
boot.init(sys.argv[1],root/'assets',crypto='stub')
verify_personal()  # A second a2 startup is idempotent.
api._close_conn()
print(json.dumps({'passed':True,'actual_device_upgrade':False,'paid_requests':0,
    'checks':['rating','remark','want','collection','categories','personal_url',
              'legacy_model','key_preserved','key_redacted','backup_preview_import','idempotent_startup']}))
"""


@pytest.mark.parametrize('version,commit', VERSIONS)
def test_historical_private_storage_upgrade(version, commit, tmp_path):
    archive = subprocess.check_output(['git','archive','--format=zip',commit],cwd=ROOT)
    old = tmp_path/'historical-source'
    old.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        z.extractall(old)
    data=tmp_path/'app-private'
    data.mkdir()
    marker=tmp_path/'expected.json'
    seed = subprocess.run([sys.executable,'-X','utf8','-c',SEED,str(data),str(marker)],cwd=old,
                          capture_output=True,text=True,encoding='utf-8',timeout=90)
    assert seed.returncode==0, (version,seed.stdout,seed.stderr)
    verify = subprocess.run([sys.executable,'-X','utf8','-c',VERIFY,str(data),str(marker)],cwd=ROOT,
                            capture_output=True,text=True,encoding='utf-8',timeout=90)
    assert verify.returncode==0, (version,verify.stdout,verify.stderr)
    assert json.loads(verify.stdout.strip().splitlines()[-1])['passed']
