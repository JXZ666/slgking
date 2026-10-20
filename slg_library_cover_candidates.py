"""Explicit cover selection without importing or rebinding game metadata.

Queries and image decode run in workers. Callers own SQLite transactions and
retain a SavedCover only AFTER commit; unused files can safely be discarded.
"""
from dataclasses import dataclass
import os
import tempfile

import slg_library_metadata as metadata
import slg_library_providers as providers
import slg_local_library as library


def search(source, query, should_stop=None):
    """Return at most ten valid, deduplicated official work candidates."""
    candidates, seen = [], set()
    for row in providers.search(source, query, should_stop=should_stop)[:10]:
        facts = metadata.validate(row)
        key = facts['source'], facts['external_id']
        if facts['source'] != source:
            raise ValueError('查询结果与所选来源不同。')
        if key not in seen:
            candidates.append(facts)
            seen.add(key)
    return candidates


@dataclass
class PreparedCover:
    candidate: dict
    image: object
    preview: object

    def close(self):
        for name in ('image', 'preview'):
            image = getattr(self, name)
            if image is not None:
                image.close()
                setattr(self, name, None)


def prepare(candidate, should_stop=None):
    """Fetch selected identity, decode full image and make bounded preview."""
    expected = metadata.validate(candidate)
    facts = metadata.validate(providers.details(expected['source'], expected['external_id'],
                                               should_stop=should_stop))
    if (facts['source'], facts['external_id']) != (expected['source'], expected['external_id']):
        raise ValueError('所选作品与返回的封面资料编号不一致。')
    if not facts['cover_url']:
        raise ValueError('该作品没有可读取的封面地址，请使用本地选图。')
    raw = providers.download_cover(facts['cover_url'], facts['source'], should_stop=should_stop)
    if should_stop and should_stop():
        raise ValueError('封面查询已取消。')
    if len(raw) > library.MAX_COVER_BYTES:
        raise ValueError('封面图片不能超过 5 MB。')
    descriptor, path = tempfile.mkstemp(suffix='.img')
    image = None
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw)
        image = library.prepare_cover(path)
        preview = image.copy()
        preview.thumbnail((240, 240))
        return PreparedCover(facts, image, preview)
    except Exception:
        if image is not None:
            image.close()
        raise
    finally:
        os.unlink(path)


@dataclass
class SavedCover:
    name: str
    path: str
    signature: tuple
    retained: bool = False

    def discard(self):
        if self.retained:
            return
        try:
            stat = os.stat(self.path, follow_symlinks=False)
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) == self.signature:
                os.unlink(self.path)
        except OSError:
            pass


def save_image(image, directory):
    """Worker-only atomic file creation; never overwrites an existing image."""
    name = library.save_cover(image, directory)
    path = os.path.join(os.fspath(directory), name)
    stat = os.stat(path, follow_symlinks=False)
    return SavedCover(name, path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))


def apply(conn, game_id, slug, saved):
    """Only the cover override changes. Commit/retain remain caller-owned."""
    row = conn.execute('SELECT slug FROM games WHERE id=?', (game_id,)).fetchone()
    if row is None or row[0] != slug:
        raise ValueError('原游戏已删除或替换，请重新打开封面窗口。')
    if not os.path.isfile(saved.path):
        raise ValueError('新封面文件已丢失，旧封面保留。')
    library.set_cover_override(conn, game_id, saved.name)


def default_query(conn, game):
    """Confirmed ID first; names are fallbacks. Never return local paths."""
    rows = conn.execute('SELECT source_id,external_id FROM game_sources WHERE game_id=?'
                        " AND source_id IN ('dlsite','vndb') ORDER BY source_id", (game['id'],)).fetchall()
    personal = metadata.get_metadata(conn, game['id'])
    for source, external in [tuple(r) for r in rows] + [(r['source'], r['external_id']) for r in personal]:
        if source in ('dlsite', 'vndb'):
            return source, external
    return 'vndb', game.get('title') or game.get('name') or ''
