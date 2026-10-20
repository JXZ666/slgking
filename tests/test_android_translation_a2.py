"""Offline Android a2 translation settings and transport regression checks."""
import io
import json
import sqlite3
import urllib.error
from unittest.mock import patch

import pytest
import slg_android_api as api
import slg_engines as engines


@pytest.fixture
def conn(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE prefs(key TEXT PRIMARY KEY,value TEXT)')
    monkeypatch.setattr(api, '_conn', lambda: db)
    monkeypatch.setattr(api, '_TRANSLATE_RUNNING', False)
    yield db
    db.close()


def save(**fields):
    return api._h_translate_save(dict(provider='deepseek', engine='openai',
        base_url='https://api.deepseek.com', model='deepseek-flash', **fields))


def seed(conn, **fields):
    for name, value in fields.items():
        conn.execute('INSERT OR REPLACE INTO prefs VALUES (?,?)', ('translate_'+name, value))
    conn.commit()


def values(conn):
    return dict(conn.execute('SELECT key,value FROM prefs'))


def test_fresh_read_is_zero_write_and_presets_single_source(conn):
    result = api._h_translate_settings({})
    assert result['model'] == 'deepseek-flash'
    assert result['presets'] == engines.translation_presets()
    assert len(result['presets']) == 7
    assert values(conn) == {}
    assert not result['has_key']
    assert 'api_key' not in result


def test_legacy_model_free_and_custom_roundtrip(conn):
    seed(conn, api_key='fake-key', model='legacy-model')
    settings = api._h_translate_settings({})
    assert settings['model'] == 'legacy-model'
    assert settings['provider'] == 'deepseek'
    assert settings['has_key']
    assert 'fake-key' not in json.dumps(settings)
    result = api._h_translate_save(dict(provider='custom', engine='openai',
        base_url='https://example.invalid/v1', model='unknown-model', api_key='new-fake-key'))
    assert result['model'] == 'unknown-model'
    assert api._h_translate_save(dict(engine='google_free'))['engine'] == 'google_free'
    assert api._h_translate_save(dict(engine='openai'))['engine'] == 'openai'


def test_same_target_blank_preserves_key_and_model_change(conn):
    save(api_key='fake-key')
    result = api._h_translate_save(dict(model='legacy-model', api_key=''))
    assert result['has_key']
    assert values(conn)['translate_api_key'] == 'fake-key'


@pytest.mark.parametrize('change', [dict(provider='openai'), dict(base_url='https://other.invalid/v1')])
def test_target_change_without_key_rejected_atomically(conn, change):
    save(api_key='fake-key')
    before = values(conn)
    result = api._h_translate_save(dict(change, api_key=''))
    assert result['ok'] is False
    assert values(conn) == before


def test_switch_free_target_clears_credential(conn):
    save(api_key='fake-key')
    assert api._h_translate_save(dict(provider='custom', base_url='', model='', engine='google_free'))['has_key'] is False


@pytest.mark.parametrize('field,value', [('provider','bad'), ('engine','bad'), ('model',''),
    ('base_url','file:///tmp/key'), ('base_url','https://u:p@example.invalid'),
    ('base_url','https://example.invalid/?api_key=fake')])
def test_invalid_config_zero_write(conn, field, value):
    save(api_key='fake-key')
    before = values(conn)
    result = api._h_translate_save({field: value})
    assert result['ok'] is False
    assert values(conn) == before


def test_sql_failure_rolls_back_all_fields(conn):
    save(api_key='fake-key')
    before = values(conn)
    conn.execute("CREATE TRIGGER fail_save BEFORE INSERT ON prefs WHEN NEW.key='translate_model' BEGIN SELECT RAISE(ABORT,'simulated'); END")
    with pytest.raises(sqlite3.IntegrityError):
        api._h_translate_save(dict(model='other', api_key='replacement', base_url='https://new.invalid'))
    assert values(conn) == before


def test_running_translation_rejects_save_and_second_task(conn, monkeypatch):
    monkeypatch.setattr(api, '_TRANSLATE_RUNNING', True)
    assert save(api_key='fake-key')['ok'] is False
    assert api._h_translate_tags_run({})['ok'] is False
    assert values(conn) == {}


@pytest.mark.parametrize('url,model,expected', [
    ('https://api.deepseek.com','deepseek-flash',True),
    ('https://api.deepseek.com/v1','deepseek-flash',True),
    ('https://api.deepseek.com','deepseek-chat',False),
    ('https://custom.invalid','deepseek-flash',False),
    ('https://api.openai.com/v1','gpt-4o-mini',False)])
def test_thinking_parameter_only_official_flash(url, model, expected):
    seen=[]
    def fetch(req, *args, **kwargs):
        seen.append(json.loads(req.data))
        return {'choices':[{'message':{'content':'ok'}}]}
    with patch.object(engines, '_fetch_json', fetch):
        assert engines.OpenAIEngine(url).chat([], 'fake-key', model) == 'ok'
    assert ('thinking' in seen[0]) == expected
    if expected: assert seen[0]['thinking'] == {'type':'disabled'}


@pytest.mark.parametrize('code', [401,429])
def test_http_error_does_not_leak_echoed_key(code):
    def fail(req, **kwargs):
        raise urllib.error.HTTPError(req.full_url, code, 'test', {},
            io.BytesIO(b'{"error":{"message":"echo fake-secret-key"}}'))
    with patch.object(engines.urllib.request,'urlopen', fail), patch.object(engines.time,'sleep'):
        with pytest.raises(engines.TranslateError) as caught:
            engines.OpenAIEngine('https://example.invalid').chat([], 'fake-secret-key','model')
    assert 'fake-secret-key' not in str(caught.value)
    assert 'HTTP' in str(caught.value)


def test_nonjson_reply_and_network_error_safe():
    with patch.object(engines,'_fetch_json', side_effect=engines.TranslateError('网络错误：fake-key')):
        with pytest.raises(engines.TranslateError) as caught:
            engines.OpenAIEngine('https://example.invalid').chat([], 'fake-key','model')
    assert 'fake-key' not in str(caught.value)
    with patch.object(engines,'_fetch_json',return_value={}):
        with pytest.raises(engines.TranslateError,match='返回结构异常'):
            engines.OpenAIEngine('https://example.invalid').chat([], 'fake-key','model')


def test_timeout_nonjson_transport_and_job_start_failure(conn, monkeypatch):
    with patch.object(engines.urllib.request, 'urlopen', side_effect=TimeoutError('timeout')):
        with pytest.raises(engines.TranslateError, match='网络错误'):
            engines.OpenAIEngine('https://example.invalid').chat([], 'fake-key', 'model')
    class Reply:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'not JSON'
    with patch.object(engines.urllib.request, 'urlopen', return_value=Reply()):
        with pytest.raises(engines.TranslateError, match='返回结构异常'):
            engines.OpenAIEngine('https://example.invalid').chat([], 'fake-key', 'model')
    monkeypatch.setattr(api, '_start_generic_job', lambda *a: (_ for _ in ()).throw(RuntimeError('test')))
    with pytest.raises(RuntimeError): api._h_translate_tags_run({})
    assert not api._TRANSLATE_RUNNING


def test_cancelling_draft_leaves_preferences_unchanged(conn):
    save(api_key='fake-key')
    before = values(conn)
    for _ in range(3): api._h_translate_settings({})
    assert values(conn) == before
