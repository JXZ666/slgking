"""Create fixtures with real old source, then verify candidate migration in isolation."""
import argparse
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
BASELINES = ('v0.25.0', '057087b', '58265ae', 'f6a82ee', 'e775e15', 'f667f3d')
sys.path.insert(0, str(REPO))

OLD_FIXTURE = '''
import json,sys
from pathlib import Path
import slg_db
c=slg_db.connect(sys.argv[1])
ids=[]
for i in range(300):
 gid,_=slg_db.upsert_game(c,'upgrade-'+str(i),'https://example.invalid/'+str(i),'Old Game '+str(i),tags=['story'])
 ids.append(gid)
slg_db.set_state(c,ids[0],status='want',my_rating=5)
if Path('slg_hidden_games.py').is_file():
 import slg_hidden_games
 slg_hidden_games.set_hidden(c,ids[2],True)
c.execute('UPDATE state SET note=? WHERE game_id=?',('fixture private note',ids[0]))
slg_db.set_local_folder(c,ids[1],sys.argv[2])
slg_db.set_local_exe(c,ids[1],sys.argv[2]+'/Game.exe')
gid=slg_db.add_user_game(c,'Fixture custom',categories=['galgame','slg'],folder_path=sys.argv[2]+'/Custom')
slg_db.set_state(c,gid,status='want',my_rating=4)
col=slg_db.create_collection(c,'Fixture collection')
slg_db.set_game_collections(c,ids[0],[col])
slg_db.add_points(c,17,'fixture private points')
slg_db.record_signin(c,'2026-09-01',3)
slg_db.own_title(c,'fixture-private-title','fixture')
slg_db.add_comment(c,'upgrade-0','fixture private comment',visibility='private')
c.execute('INSERT INTO exclusions(tag,source) VALUES (?,?)',('fixture-excluded','user'))
slg_db.add_alias(c,ids[0],'fixture private alias')
slg_db.set_manual_translation(c,'title',ids[0],'fixture private translated title')
slg_db.set_pref(c,'ui.reduced_motion','1')
slg_db.set_pref(c,'ui.font_size','15')
slg_db.set_local_save(c,sys.argv[2]+'/saves',game_slug='upgrade-1',display_title='Fixture saves',engine='RenPy',save_count=3,total_size=1234)
if Path('slg_local_library.py').is_file():
 import slg_local_library as library
 from PIL import Image
 cover=Path(sys.argv[2])/'fixture-cover.png'
 Image.new('RGB',(32,48),'blue').save(cover)
 library.set_cover_override(c,ids[1],library.import_cover(str(cover)))
if Path('slg_scan_targets.py').is_file():
 import slg_scan_targets
 slg_scan_targets.save_targets(c,[dict(path=sys.argv[2],mode='single')])
c.commit()
raw={table:[dict(row) for row in c.execute('SELECT * FROM '+table)] for table in ('local','local_saves','game_aliases','prefs','comments')}
json.dump({'ids':ids,'custom':gid,'data':slg_db.export_user_data(c),'raw_personal':raw},open(sys.argv[3],'w',encoding='utf-8'),ensure_ascii=False)
c.close()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', type=Path)
    parser.add_argument('--keep', action='store_true', help='retain isolated old profiles for EXE smoke')
    args = parser.parse_args()
    import slg_db
    import slg_sync_server
    results = []
    root = Path(tempfile.mkdtemp(prefix='slg-0265-upgrade-'))
    manifest = json.loads((REPO/'assets/dlsite/manifest.json').read_text(encoding='utf-8'))
    expected_dlsite = manifest['count']
    for ref in BASELINES:
        folder = root / ref.replace('.', '-')
        source, profile = folder / 'source', folder / 'profile'
        source.mkdir(parents=True)
        profile.mkdir()
        archive = subprocess.check_output(['git', 'archive', '--format=zip', ref], cwd=REPO)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            bundle.extractall(source)
        env = dict(os.environ)
        for key in ('LOCALAPPDATA', 'APPDATA', 'HOME', 'USERPROFILE'):
            env[key] = str(profile)
        game_folder = folder / 'games'
        game_folder.mkdir()
        (game_folder / 'Game.exe').touch()
        db = profile / 'slgking' / 'slgking.db'
        db.parent.mkdir()
        fixture = folder / 'fixture.json'
        subprocess.run([sys.executable, '-c', OLD_FIXTURE, str(db), str(game_folder), str(fixture)],
                       cwd=source, env=env, check=True, capture_output=True)
        before = json.loads(fixture.read_text(encoding='utf-8'))
        # Retain an unupgraded profile for actual frozen-runtime migration,
        # independently of the source migration checked below.
        runtime_profile = folder / 'runtime-profile'
        shutil.copytree(profile, runtime_profile)
        profile_env = patch.dict(os.environ, {key: str(profile) for key in ('LOCALAPPDATA','APPDATA','HOME','USERPROFILE')})
        profile_env.start()
        conn = None
        try:
            conn = slg_db.connect(str(db))
            slg_sync_server.import_local_catalogue(conn, REPO/'assets/dlsite/slgking.db',
                REPO/'assets/dlsite/manifest.json', log=lambda *_: None)
            conn.commit()
            after = slg_db.export_user_data(conn)
            for section, expected in before['data'].items():
                assert after[section] == expected, (ref, section, expected, after[section])
            for table, expected in before['raw_personal'].items():
                actual = [dict(row) for row in conn.execute('SELECT * FROM '+table)]
                assert all(any(all(row.get(key)==value for key,value in old.items()) for row in actual) for old in expected), (ref, table)
            assert [r[0] for r in conn.execute("SELECT id FROM games WHERE slug LIKE 'upgrade-%' ORDER BY id")] == before['ids']
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert len(slg_db.find_games(conn, catalog_id='dlsite', sort='title', with_scores=False)) == expected_dlsite
            assert slg_db.points_balance(conn) == 20, (ref, 'trusted points balance')
            assert 'fixture-private-title' in slg_db.owned_title_ids(conn), (ref, 'trusted owned title')
            results.append(dict(baseline=ref, old_games=301, imported_dlsite=expected_dlsite,
                                preserved_sections=sorted(before['data']),
                                raw_personal_tables_preserved=sorted(before['raw_personal']),
                                personal_sections_preserved=True, ids_preserved=True,
                                integrity='ok', trusted_points_balance=20, trusted_title_preserved=True,
                                profile=str(profile), database=str(db),
                                runtime_profile=str(runtime_profile), fixture=str(fixture)))
        finally:
            if conn is not None:
                conn.close()
            profile_env.stop()
    output = json.dumps(dict(fixtures=str(root), results=results), ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output+'\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
