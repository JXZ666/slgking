"""Explicitly selected Steam library inspection; never executes discovered files."""
from pathlib import Path
import re
import os

def _linked(path):
    return path.is_symlink() or bool(getattr(os.path,"isjunction",lambda p:False)(path))

class SteamManifestError(ValueError):
    pass

def _parse(text):
    tokens = re.findall(r'"((?:\\.|[^"\\])*)"|([{}])|(\S+)', text)
    stream=[]
    for value, brace, invalid in tokens:
        if invalid:
            raise SteamManifestError('Steam manifest格式无效')
        stream.append(brace or value)
    index=0
    def obj(depth=0):
        nonlocal index
        if depth>12: raise SteamManifestError('manifest嵌套过深')
        result={}
        while index<len(stream):
            key=stream[index]; index+=1
            if key=='}': return result
            if key=='{' or index>=len(stream): raise SteamManifestError('manifest结构不完整')
            value=stream[index]; index+=1
            if value=='{': value=obj(depth+1)
            elif value=='}': raise SteamManifestError('manifest结构不完整')
            if key in result: raise SteamManifestError('manifest重复字段')
            result[key]=value
        if depth: raise SteamManifestError('manifest缺少结束括号')
        return result
    result=obj()
    if index!=len(stream): raise SteamManifestError('manifest尾部无效')
    return result

def inspect_library(library, should_stop=None, max_manifests=5000):
    base=Path(library).resolve(strict=True)
    steamapps=base if base.name.lower()=='steamapps' else base/'steamapps'
    if not steamapps.is_dir() or _linked(steamapps) or not steamapps.resolve().is_relative_to(base):
        raise SteamManifestError('请选择包含steamapps的Steam游戏库')
    rows=[]; failures=[]; seen=set()
    for index,path in enumerate(sorted(steamapps.glob('appmanifest_*.acf'))):
        if should_stop and should_stop(): return {'candidates':rows,'failures':failures,'cancelled':True}
        if index>=max_manifests:
            failures.append({'path':str(steamapps),'reason':'manifest数量超过上限'}); break
        try:
            if _linked(path) or not path.resolve().is_relative_to(steamapps.resolve()) or path.stat().st_size>1024*1024:
                raise SteamManifestError('manifest路径或大小无效')
            expected=re.fullmatch(r'appmanifest_([1-9][0-9]{0,9})\.acf',path.name)
            if not expected: raise SteamManifestError('manifest编号无效')
            app=_parse(path.read_text(encoding='utf-8-sig'))['AppState']
            if not isinstance(app,dict) or app.get('appid')!=expected[1]: raise SteamManifestError('manifest编号不一致')
            name=app.get('name',''); install=app.get('installdir','')
            if not name or len(name)>512 or not install or len(install)>255 or any(c in install for c in '/\\:') or install in ('.','..'):
                raise SteamManifestError('manifest名称或安装目录无效')
            root=steamapps/'common'/install
            if (not root.is_dir() or _linked(root) or _linked(steamapps/'common')
                    or not root.resolve().is_relative_to(base)
                    or not root.resolve().is_relative_to((steamapps/'common').resolve())):
                raise SteamManifestError('安装目录不存在或越界')
            identity=app['appid']
            if identity in seen: continue
            seen.add(identity)
            from slg_source_registry import official_url
            rows.append(dict(source='steam',external_id=identity,title=name,source_url=official_url('steam',identity),
                             folder_path=str(root.resolve()),manifest_path=str(path),requires_confirmation=True,
                             match_reason='Steam本机manifest；请确认游戏目录',cover_url='',developer='',release_date='',original_title=''))
        except (OSError,UnicodeError,KeyError,ValueError) as exc:
            failures.append({'path':str(path),'reason':str(exc) if isinstance(exc,SteamManifestError) else '无法读取有效Steam manifest'})
    return {'candidates':rows,'failures':failures,'cancelled':False}

def appid_hint(folder):
    path=Path(folder)/'steam_appid.txt'
    if _linked(path) or not path.is_file() or path.stat().st_size>64: return None
    try:value=path.read_text(encoding='ascii').strip()
    except (OSError,UnicodeError):return None
    if not re.fullmatch(r'[1-9][0-9]{0,9}',value): return None
    return dict(source='steam',external_id=value,requires_confirmation=True,
                match_reason='开发用steam_appid.txt，仅作候选，不能确认作品',test_appid=value=='480')
