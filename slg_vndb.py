"""VNDB public facts. Native 10..100 ratings never enter legacy XP columns."""
import copy
import math
import re
from urllib.parse import urlsplit

FIELDS = "title,titles{lang,title,latin,official,main},aliases,released,languages,platforms,rating,average,votecount,description,developers{id,name},image{id,url,dims,sexual,violence,votecount,thumbnail,thumbnail_dims},extlinks{url,label,name,id}"


def identity(value):
    if not isinstance(value,str) or not re.fullmatch(r"v[1-9]\d*",value):
        raise ValueError("Invalid VNDB id")
    return value


def validate_metadata(value):
    if not isinstance(value,dict):
        raise ValueError("Invalid VNDB metadata")
    identity(value.get("vndb_id"))
    allowed = {'vndb_id','rating_value','average','rating_scale_max','rating_count','release_date','titles','aliases','languages','platforms','developers','image','external_links'}
    if set(value)-allowed or value.get('rating_scale_max') != 100:
        raise ValueError('Unknown VNDB facts or rating scale')
    for field in ("rating_value","average","rating_scale_max","rating_count"):
        number=value.get(field)
        if number is not None and (type(number) not in (int,float) or not math.isfinite(number) or number<0):
            raise ValueError("Invalid native VNDB rating")
    if value.get("rating_value") is not None and not 10<=value["rating_value"]<=100:
        raise ValueError("Invalid VNDB native scale")
    if value.get("rating_count") is not None and type(value["rating_count"]) is not int:
        raise ValueError("Invalid vote count")
    for key in ("languages","platforms","titles","aliases","developers","external_links"):
        if key in value and not isinstance(value[key],list):
            raise ValueError("Invalid VNDB list")
    for key in ('languages','platforms','aliases'):
        if any(not isinstance(item,str) or len(item)>4096 for item in value.get(key,[])):
            raise ValueError('Invalid VNDB text list')
    for key in ('titles','developers','external_links'):
        if any(not isinstance(item,dict) for item in value.get(key,[])):
            raise ValueError('Invalid VNDB object list')
    if value.get("release_date") is not None and not re.fullmatch(r"\d{4}(?:-\d{2})?(?:-\d{2})?|TBA|unknown",value["release_date"]):
        raise ValueError("Invalid partial release date")
    image=value.get("image")
    if image:
        if not isinstance(image,dict):
            raise ValueError('Invalid VNDB image')
        parsed=urlsplit(image.get("url", ""))
        if parsed.scheme!="https" or parsed.hostname!="t.vndb.org" or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError("Invalid official cover host")
        for key in ("sexual","violence"):
            score=image.get(key)
            if score is not None and (type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=2):
                raise ValueError("Invalid image content mark")
    return copy.deepcopy(value)


def normalize_work(raw):
    vid=identity(raw.get("id"))
    title=raw.get("title")
    if not isinstance(title,str) or not title.strip():
        raise ValueError("Missing VNDB title")
    metadata=validate_metadata({"vndb_id":vid,"rating_value":raw.get("rating"),"average":raw.get("average"),"rating_scale_max":100,"rating_count":raw.get("votecount"),"release_date":raw.get("released"),"titles":raw.get("titles",[]),"aliases":raw.get("aliases",[]),"languages":raw.get("languages",[]),"platforms":raw.get("platforms",[]),"developers":raw.get("developers",[]),"image":raw.get("image"),"external_links":raw.get("extlinks",[])})
    return {"slug":"vndb-"+vid,"url":"https://vndb.org/"+vid,"title":title.strip(),"developer":" / ".join(d["name"] for d in metadata["developers"] if isinstance(d,dict) and isinstance(d.get("name"),str)),"overview":raw.get("description"),"metadata":metadata}


def hide_sensitive_cover(metadata, enabled):
    image=metadata.get("image") or {}
    return bool(enabled and any(image.get(key) is None or image[key] > 0
                                for key in ('sexual', 'violence')))


def detail_lines(metadata):
    """Plain fact rows for GUI, preserving source scales and partial dates."""
    if not metadata:
        return []
    value = validate_metadata(metadata)
    result = [('VNDB编号', value['vndb_id']), ('发布日期', value.get('release_date') or '未知')]
    if value.get('rating_value') is not None:
        result.append(('VNDB评分', '%g / 100（%s票）' % (value['rating_value'], value.get('rating_count', '未知'))))
    result.append(('语言', '、'.join(value.get('languages', [])) or '未知'))
    result.append(('平台', '、'.join(value.get('platforms', [])) or '未知'))
    return result


def association_suggestions(conn, game_id, *, include_hidden=False):
    """Evidence-only cross-source suggestions. Never merge or write records."""
    import json
    import slg_hidden_games
    row = conn.execute('SELECT metadata_json FROM game_metadata WHERE game_id=?', (game_id,)).fetchone()
    if not row:
        return []
    raw = json.loads(row[0])
    if not isinstance(raw,dict) or 'vndb_id' not in raw:
        return []
    facts = validate_metadata(raw)
    results = {}
    for link in facts.get('external_links', []):
        # A displayed title is not an identity. The exact official external
        # link and its DLsite (RJ or VJ) identifier must agree before
        # consulting mappings.
        if link.get('name') != 'dlsite':
            continue
        external = str(link.get('id', '')).upper()
        if not re.fullmatch(r'(?:RJ|VJ)\d{6,10}', external):
            continue
        parsed = urlsplit(link.get('url', ''))
        # RJ works live under /maniax/, VJ works under /pro/. The section and
        # the identifier must agree, so a mismatched link is not evidence.
        section = 'maniax' if external.startswith('RJ') else 'pro'
        if (parsed.scheme != 'https' or parsed.hostname != 'www.dlsite.com'
                or parsed.username or parsed.password or parsed.port not in (None,443)
                or not re.search(r'/' + section + r'/work/=/product_id/'
                                 + re.escape(external) + r'\.html(?:/|$)',
                                 parsed.path, re.I)):
            continue
        for candidate, title in conn.execute("SELECT g.id,g.title FROM game_sources s JOIN games g ON g.id=s.game_id WHERE s.source_id='dlsite' AND s.external_id=? AND g.origin='site' AND g.id<>?", (external,game_id)):
            if not include_hidden and slg_hidden_games.is_hidden(conn,candidate):
                continue
            results[candidate] = dict(game_id=candidate,title=title,source='dlsite',external_id=external,evidence_url=link['url'],requires_confirmation=True)
    return list(results.values())


def confirm_association(conn, left_id, right_id):
    """Persist only the explicitly confirmed relation, never canonicalize IDs."""
    import datetime
    if type(left_id) is not int or type(right_id) is not int or left_id == right_id:
        raise ValueError('Choose two different games')
    pair = None
    for owner, candidate in ((left_id,right_id),(right_id,left_id)):
        for suggestion in association_suggestions(conn,owner,include_hidden=True):
            if suggestion['game_id'] == candidate:
                pair=(owner,candidate,suggestion['evidence_url'])
                break
        if pair:
            break
    if pair is None:
        raise ValueError('No explicit official identity evidence for association')
    left,right,url=pair
    confirmed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    conn.execute('INSERT OR IGNORE INTO local_catalogue_links VALUES (?,?,?,?)',(left,right,url,confirmed_at))
    return confirmed_associations(conn,left)


def confirmed_associations(conn, game_id):
    import slg_hidden_games
    return [dict(game_id=other,title=title,slug=slug,evidence_url=url,confirmed_at=at,hidden=bool(slg_hidden_games.is_hidden(conn,other)))
        for other,title,slug,url,at in conn.execute('SELECT g.id,g.title,g.slug,l.evidence_url,l.confirmed_at FROM local_catalogue_links l JOIN games g ON g.id=CASE WHEN l.left_id=? THEN l.right_id ELSE l.left_id END WHERE l.left_id=? OR l.right_id=? ORDER BY g.slug',(game_id,game_id,game_id))]


ASSOCIATION_BACKUP_FIELDS = {'left_slug','right_slug'}


def export_associations(conn):
    return [dict(left_slug=left,right_slug=right) for left,right in conn.execute('SELECT a.slug,b.slug FROM local_catalogue_links l JOIN games a ON a.id=l.left_id JOIN games b ON b.id=l.right_id ORDER BY a.slug,b.slug')]


def validate_associations(records, conn=None):
    if not isinstance(records,list):
        raise ValueError('Invalid local association backup')
    for record in records:
        if not isinstance(record,dict) or set(record)!=ASSOCIATION_BACKUP_FIELDS:
            raise ValueError('Invalid local association fields')
        left,right=record['left_slug'],record['right_slug']
        if not isinstance(left,str) or not isinstance(right,str) or left==right:
            raise ValueError('Invalid local association identities')
        if not ((re.fullmatch(r'vndb-v[1-9]\d*',left) and re.fullmatch(r'dlsite-(?:rj|vj)\d{6,10}',right)) or (re.fullmatch(r'vndb-v[1-9]\d*',right) and re.fullmatch(r'dlsite-(?:rj|vj)\d{6,10}',left))):
            raise ValueError('Association requires explicit supported source identities')
        if conn is not None:
            ids={row[1]:row[0] for row in conn.execute('SELECT id,slug FROM games WHERE slug IN (?,?)',(left,right))}
            if len(ids)!=2:
                raise ValueError('Import required public catalogues before associations')
            if not any(item['game_id']==ids[right] for item in association_suggestions(conn,ids[left],include_hidden=True)) and not any(item['game_id']==ids[left] for item in association_suggestions(conn,ids[right],include_hidden=True)):
                raise ValueError('Association evidence no longer exists')


def import_associations(conn, records):
    # Missing old sections restore no links; never silently invent relations.
    validate_associations(records,conn)
    conn.execute('DELETE FROM local_catalogue_links')
    for record in records:
        ids={row[1]:row[0] for row in conn.execute('SELECT id,slug FROM games WHERE slug IN (?,?)',(record['left_slug'],record['right_slug']))}
        confirm_association(conn,ids[record['left_slug']],ids[record['right_slug']])
