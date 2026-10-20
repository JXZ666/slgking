"""Bounded, read-only game fingerprints and Windows launch candidates.

Detection is a suggestion; it never changes stored or manually selected engines.
Save discovery is deliberately outside this module.
"""
import os
import re
from collections import deque
from pathlib import Path

MAX_DEPTH = 4
MAX_ENTRIES = 5000
ENGINE_CHOICES = ("Ren'Py", "Unity", "Unreal Engine", "RPG Maker", "Godot",
                  "TyranoBuilder", "Kirikiri", "HTML", "WOLF", "LiveMaker", "Bakin")
SKIP_DIRS = frozenset({".git", ".svn", "__pycache__", "node_modules", ".cache",
                       "cache", "caches", ".godot", "shadercache", "gpucache", "mtool", "loaders", "tools", "tool", "swiftshader"})
_NON_GAME_EXE = re.compile(r"^(?:unins\d*|uninstall.*|crash.*|unitycrashhandler.*|"
                           r"vc_redist.*|dxsetup|setup|install.*|.*crashreportclient.*|opensavefolder|resetconfig|inject|chromedriver|notification_helper)$", re.I)


def detect_game(folder, should_stop=None, *, max_depth=None, max_entries=None):
    """Return engine, engines, evidence, executables and warnings.

    Paths in evidence are relative; executable paths are absolute. Empty engine
    means unknown, ambiguous or cancelled (see engines/warnings). Defaults inspect
    four entry levels and 5000 entries; configurable depth is capped at ten.
    Multiple game roots never become one automatic suggestion. No file contents
    are read; engine_variant is optional and does not change the engine category.
    """
    stop = should_stop or (lambda: False)
    depth_limit = MAX_DEPTH if max_depth is None else max(1, min(10, int(max_depth)))
    entry_limit = MAX_ENTRIES if max_entries is None else max(1, min(5000, int(max_entries)))
    root = Path(os.path.abspath(os.fspath(folder)))
    files, dirs, warnings = {}, {}, []
    cancelled = False
    exhausted = False
    count = 0

    def warn(message):
        if message not in warnings:
            warnings.append(message)

    try:
        if root.is_symlink() or getattr(os.path, 'isjunction', lambda p: False)(root):
            warn("symlink_skipped: root")
            return _result([], [], warnings)
    except OSError as exc:
        warn("unreadable: root ({})".format(type(exc).__name__))
        return _result([], [], warnings)
    queue = deque([(root, (), 0)])
    while queue and not exhausted:
        if stop():
            cancelled = True
            break
        path, parts, depth = queue.popleft()
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if stop():
                        cancelled = True
                        break
                    if count >= entry_limit:
                        exhausted = True
                        warn("entry_limit: {}".format(entry_limit))
                        break
                    count += 1
                    relative = parts + (entry.name,)
                    key = tuple(p.casefold() for p in relative)
                    try:
                        if entry.is_symlink() or getattr(os.path, 'isjunction', lambda p: False)(entry.path):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.casefold() in SKIP_DIRS:
                                continue
                            dirs[key] = relative
                            if depth + 1 < depth_limit:
                                queue.append((Path(entry.path), relative, depth + 1))
                            else:
                                warn("depth_limit: {}".format(depth_limit))
                        elif entry.is_file(follow_symlinks=False):
                            files[key] = relative
                    except OSError as exc:
                        warn("unreadable: {} ({})".format('/'.join(relative), type(exc).__name__))
        except OSError as exc:
            warn("unreadable: {} ({})".format('/'.join(parts) or 'root', type(exc).__name__))
        if cancelled:
            break
    if cancelled:
        warn("cancelled")
        return _result([], [], warnings)

    by_parent = {}
    for key in files:
        by_parent.setdefault(key[:-1], []).append(key)
    evidence = []
    specialized_html = []
    parents = sorted(set(dirs) | {key[:-1] for key in files} | {()})
    def has(base, name):
        return base + tuple(name.casefold().split('/')) in files
    def directory(base, name):
        return base + tuple(name.casefold().split('/')) in dirs
    def children(base, suffix):
        return sorted(k for k in by_parent.get(base, ()) if k[-1].endswith(suffix))
    def add(engine, rule, keys, base=None, variant="", game_root=None):
        evidence.append({"engine": engine, "rule": rule,
                         "paths": ['/'.join(files.get(k, dirs.get(k, k))) for k in keys],
                         "engine_variant": variant,
                         "game_root": "/".join(dirs.get(game_root, game_root)) if game_root else "."})
        if base is not None:
            specialized_html.append(base)

    for base in parents:
        if stop():
            warn('cancelled')
            return _result([], [], warnings)
        exes = children(base, '.exe')
        playable = [k for k in exes if not _NON_GAME_EXE.fullmatch(k[-1][:-4])]
        renpy_scripts = children(base + ('game',), ('.rpy', '.rpyc', '.rpa'))
        if has(base, 'renpy/__init__.py') and renpy_scripts:
            add("Ren'Py", "renpy runtime + game script/archive", [base + ('renpy', '__init__.py'), renpy_scripts[0]], game_root=base)
        for exe in playable:
            data = base + (exe[-1][:-4] + '_data',)
            marker = next((data + (name,) for name in ('globalgamemanagers', 'data.unity3d')
                           if data + (name,) in files), None)
            if has(base, 'UnityPlayer.dll') and data in dirs and marker:
                add('Unity', 'UnityPlayer + matching EXE/data + Unity data marker',
                    [exe, base + ('unityplayer.dll',), marker], game_root=base,
                    variant="IL2CPP" if has(base, "GameAssembly.dll") and has(data, "il2cpp_data/Metadata/global-metadata.dat") else "")
        win64 = base + ('binaries', 'win64')
        ue_exes = [k for k in children(win64, '.exe')
                   if not _NON_GAME_EXE.fullmatch(k[-1][:-4])]
        paks = children(base + ('content', 'paks'), '.pak')
        if ue_exes and paks:
            add('Unreal Engine', 'Binaries/Win64 EXE + Content/Paks PAK', [ue_exes[0], paks[0]], game_root=base)
        for web in (base, base + ('www',)):
            core = next((name for name in ('js/rpg_core.js', 'js/rmmz_core.js') if has(web, name)), None)
            if core and has(web, 'data/System.json') and has(web, 'index.html'):
                add('RPG Maker', 'RPG core + System.json + index.html',
                    [web + tuple(core.split('/')), web + ('data', 'system.json'), web + ('index.html',)], web, variant='MZ' if 'rmmz' in core else 'MV', game_root=base[:-1] if base and base[-1] == 'www' else base)
        archives = children(base, ('.rgssad', '.rgss2a', '.rgss3a'))
        if playable and archives and has(base, 'Game.ini'):
            add('RPG Maker', 'RGSS archive + Game.ini + EXE', [archives[0], base + ('game.ini',), playable[0]], game_root=base,
                variant={'.rgssad': 'XP', '.rgss2a': 'VX', '.rgss3a': 'VX Ace'}[next(suffix for suffix in ('.rgssad', '.rgss2a', '.rgss3a') if archives[0][-1].endswith(suffix))])
        rgss_data = children(base + ('data',), ('.rxdata', '.rvdata', '.rvdata2'))
        rgss_dlls = [k for k in children(base, '.dll') + children(base + ('system',), '.dll')
                     if re.fullmatch(r'rgss[0-9]+[a-z]*\.dll', k[-1])]
        if playable and rgss_data and rgss_dlls and has(base, 'Game.ini'):
            add('RPG Maker', 'RGSS runtime + unencrypted data + Game.ini + EXE',
                [rgss_dlls[0], rgss_data[0], base + ('game.ini',), playable[0]], game_root=base,
                variant='VX Ace' if rgss_data[0][-1].endswith('.rvdata2') else 'VX' if rgss_data[0][-1].endswith('.rvdata') else 'XP')
        if has(base, 'project.godot') and children(base, '.tscn'):
            add('Godot', 'project.godot + scene', [base + ('project.godot',), children(base, '.tscn')[0]], game_root=base)
        for exe in playable:
            pck = exe[:-1] + (exe[-1][:-4] + '.pck',)
            if pck in files:
                add('Godot', 'matching EXE/PCK export', [exe, pck], game_root=base)
        if all(has(base, name) for name in ('index.html', 'tyrano/tyrano.js', 'data/system/config.tjs')):
            add('TyranoBuilder', 'Tyrano runtime + config + index.html',
                [base + ('index.html',), base + ('tyrano', 'tyrano.js'), base + ('data', 'system', 'config.tjs')], base, game_root=base)
        xp3 = children(base, '.xp3')
        krkr = [k for k in playable if k[-1].startswith(('krkr', 'kirikiri'))]
        plugins = [k for k in children(base + ('plugin',), '.dll')
                   if k[-1].startswith('krkr')]
        if xp3 and playable and (krkr or plugins):
            add('Kirikiri', 'XP3 + EXE + named Kirikiri runtime/plugin', [xp3[0], (krkr or plugins)[0], playable[0]], game_root=base)
        wolf_data = children(base + ('data',), '.wolf') + children(base, '.wolf')
        if playable and ((has(base, 'Data/BasicData/Game.dat') and (wolf_data or has(base, 'Data/BasicData/SysDatabase.dat'))) or (has(base, 'data.wolf') and has(base, 'Game.ini'))):
            add('WOLF', 'EXE + WOLF Game.dat + archive/system database',
                [playable[0], base + (('data', 'basicdata', 'game.dat') if has(base, 'Data/BasicData/Game.dat') else ('game.ini',)), wolf_data[0] if wolf_data else base + ('data', 'basicdata', 'sysdatabase.dat')], game_root=base)
        lm = children(base, '.dat')
        if playable and has(base, 'live.dll') and has(base, 'setting.ini') and lm:
            add('LiveMaker', 'EXE + live.dll + setting.ini + data archive', [playable[0], base + ('live.dll',), base + ('setting.ini',), lm[0]], game_root=base)
        if has(base, 'BakinPlayer.exe') and has(base, 'GameConfig.sbcf') and has(base, 'libSkiaSharp.dll'):
            add('Bakin', 'BakinPlayer + game config + rendering runtime', [base + ('bakinplayer.exe',), base + ('gameconfig.sbcf',), base + ('libskiasharp.dll',)], game_root=base)
        if playable and has(base, 'bakinplayer.dll') and (has(base, 'game.bakin') or has(base, 'data/game.bakin')):
            marker = base + (('game.bakin',) if has(base, 'game.bakin') else ('data', 'game.bakin'))
            add('Bakin', 'EXE + BakinPlayer runtime + game project', [playable[0], base + ('bakinplayer.dll',), marker], game_root=base)
    for key in sorted(files):
        if stop():
            warn('cancelled')
            return _result([], [], warnings)
        embedded_data = any(
            item['engine'] != 'HTML'
            and key[:-1][:len(base)] == base
            and any(part in ('data', 'assets', 'content', 'renpy', 'js', 'tyrano') or part.endswith('_data')
                    for part in key[:-1][len(base):])
            for item in evidence
            for base in [tuple(part.casefold() for part in item['game_root'].split('/')) if item['game_root'] != '.' else ()]
        )
        if key[-1] == 'index.html' and not embedded_data and not any(key[:-1] == b for b in specialized_html):
            add('HTML', 'index.html entry point', [key], game_root=key[:-1])
    executables = [str(root.joinpath(*relative)) for key, relative in sorted(files.items(), key=lambda item: (len(item[0]), item[0]))
                   if key[-1].endswith('.exe') and not _NON_GAME_EXE.fullmatch(key[-1][:-4])]
    if len(executables) > 1:
        warn('multiple_executables: user selection required')
    roots = {item['game_root'] for item in evidence}
    if len(roots) > 1:
        warn('multiple_game_roots: user selection required')
    return _result(evidence, executables, warnings)


def _result(evidence, executables, warnings):
    engines = [engine for engine in ENGINE_CHOICES if any(e['engine'] == engine for e in evidence)]
    if len(engines) > 1:
        warnings.append('ambiguous_engines: user selection required')
    complete = not any(w.startswith(('entry_limit:', 'unreadable:', 'cancelled', 'multiple_game_roots:')) for w in warnings)
    variants = {item.get('engine_variant', '') for item in evidence} if len(engines) == 1 else set()
    return {'engine': engines[0] if len(engines) == 1 and complete else '', 'engines': engines,
            'engine_variant': next(iter(variants)) if len(variants) == 1 else '',
            'evidence': evidence, 'executables': executables, 'warnings': warnings}


def detect_engine(folder):
    """Legacy single string suggestion; unknown/ambiguous/cancelled yields ''."""
    return detect_game(folder)['engine']
