"""Single client-side capability and identity registry; no credentials or networking."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

@dataclass(frozen=True)
class Source:
    label: str
    id_pattern: str
    hosts: frozenset
    cover_hosts: frozenset
    search: bool = True
    details: bool = True
    cover: bool = True

SOURCES = {
 'dlsite': Source('DLsite', r'(?:RJ|VJ)[0-9]{6,10}', frozenset({'www.dlsite.com'}), frozenset({'img.dlsite.jp','img.dlsite.com','media.dlsite.com'})),
 'vndb': Source('VNDB', r'v[1-9][0-9]*', frozenset({'vndb.org','api.vndb.org'}), frozenset({'t.vndb.org'})),
 'bangumi': Source('Bangumi', r'[1-9][0-9]{0,9}', frozenset({'bgm.tv','api.bgm.tv'}), frozenset({'lain.bgm.tv'})),
 'steam': Source('Steam', r'[1-9][0-9]{0,9}', frozenset({'store.steampowered.com'}), frozenset(), False, False, False),
}

def valid_id(source, value):
    return source in SOURCES and isinstance(value,str) and bool(re.fullmatch(SOURCES[source].id_pattern,value))

def official_url(source, identity):
    if not valid_id(source,identity):
        raise ValueError('资料来源或编号无效')
    if source == 'dlsite':
        return f'https://www.dlsite.com/{"pro" if identity.startswith("VJ") else "maniax"}/work/=/product_id/{identity}.html'
    return {'vndb':f'https://vndb.org/{identity}', 'bangumi':f'https://bgm.tv/subject/{identity}', 'steam':f'https://store.steampowered.com/app/{identity}/'}[source]

def valid_url(source, url, cover=False):
    try:
        item=SOURCES[source]; parsed=urlsplit(url)
        return (parsed.scheme=='https' and parsed.hostname in (item.cover_hosts if cover else item.hosts)
                and not parsed.username and not parsed.password and parsed.port in (None,443) and not parsed.fragment)
    except (ValueError,KeyError,TypeError):
        return False

def valid_identity_url(source, identity, url):
    if not valid_id(source,identity) or not valid_url(source,url):
        return False
    return urlsplit(url).path.rstrip('/') == urlsplit(official_url(source,identity)).path.rstrip('/')
