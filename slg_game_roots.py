"""Bounded game-root previews; no execution, registration or database access."""
import os
from pathlib import Path
from slg_game_detection import detect_game
from slg_scan_evidence import extract_evidence


def discover_game_roots(folder, should_stop=None, *, max_depth=4, max_entries=5000, max_roots=80):
    """Return roots/fallback/warnings/cancelled using a single shared directory inventory.

    Depth counts entry levels (same as detect_game); limits are clamped to 10,
    5000 and 80. Partial limits are reported and require explicit user review.
    Metadata files are bounded by extract_evidence, with no second directory walk.
    """
    stop = should_stop or (lambda: False)
    root = Path(os.path.abspath(os.fspath(folder)))
    found = detect_game(root, stop, max_depth=max_depth, max_entries=max_entries)
    warnings = list(found['warnings'])
    if 'cancelled' in warnings or stop():
        return {'roots': [], 'fallback': None, 'warnings': list(dict.fromkeys(warnings + ['cancelled'])), 'cancelled': True}
    grouped = {}
    for item in found['evidence']:
        relative = item.get('game_root', '.')
        grouped.setdefault(relative, []).append(item)
    limit = max(1, min(80, int(max_roots)))
    ordered = sorted(grouped, key=lambda value: (len(Path(value).parts), value.casefold()))
    if len(ordered) > limit:
        warnings.append('root_limit: {}'.format(limit))
    roots = []
    all_paths = {relative: root / relative for relative in ordered}
    for relative in ordered[:limit]:
        if stop():
            return {'roots': [], 'fallback': None, 'warnings': warnings + ['cancelled'], 'cancelled': True}
        path = all_paths[relative]
        evidence = grouped[relative]
        engines = list(dict.fromkeys(item['engine'] for item in evidence))
        variants = set(item.get('engine_variant', '') for item in evidence)
        executables = []
        for value in found['executables']:
            executable = Path(value)
            if path not in executable.parents:
                continue
            # A launch candidate belongs to the deepest discovered game root.
            if any(other != path and path in other.parents and other in executable.parents for other in all_paths.values()):
                continue
            executables.append(value)
        metadata = extract_evidence(path, executables, stop, enumerate_urls=False)
        if stop():
            return {'roots': [], 'fallback': None, 'warnings': warnings + ['cancelled'], 'cancelled': True}
        local_warnings = list(metadata['warnings'])
        if len(engines) > 1:
            local_warnings.append('ambiguous_engines: user selection required')
        if len(executables) > 1:
            local_warnings.append('multiple_executables: user selection required')
        roots.append({'path': str(path), 'engine': engines[0] if len(engines) == 1 else '',
                      'engines': engines, 'engine_variant': next(iter(variants)) if len(variants) == 1 else '',
                      'evidence': evidence, 'executables': executables,
                      'title_evidence': metadata['titles'], 'identities': metadata['identities'],
                      'warnings': local_warnings})
    fallback = None
    if not roots and not any(w.startswith('symlink_skipped: root') for w in warnings):
        # Reuse the existing inventory: never scan unknown roots a second time.
        metadata = extract_evidence(root, found['executables'], stop, enumerate_urls=False)
        if stop():
            return {'roots': [], 'fallback': None, 'warnings': warnings + ['cancelled'], 'cancelled': True}
        if found['executables'] or metadata['titles']:
            local_warnings = list(dict.fromkeys(warnings + metadata['warnings'] +
                                              ['unknown_engine: user confirmation required']))
            fallback = {'path': str(root), 'engine': '', 'engines': [], 'engine_variant': '',
                        'evidence': [], 'executables': list(found['executables']),
                        'title_evidence': metadata['titles'], 'identities': metadata['identities'],
                        'warnings': local_warnings}
    return {'roots': roots, 'fallback': fallback, 'warnings': warnings, 'cancelled': False}
