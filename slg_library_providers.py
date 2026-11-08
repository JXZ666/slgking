"""User-initiated metadata lookup. No catalogue crawling or automatic import.

Uses the system proxy like other third-party client requests. HTTP errors are
authoritative; no direct-route fallback or automatic retries. Bangumi accepts an
explicitly provided local PAT; other sources never receive it.
"""
import copy
from slg_source_registry import SOURCES, valid_id, valid_url
import email.utils
import html
from html.parser import HTMLParser
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import urllib.robotparser

UA = 'JXZ666/SLGking/0.28.5 (https://github.com/JXZ666/slgking)'
TIMEOUT = 15
MAX_RESPONSE = 2 * 1024 * 1024
MAX_COVER = 5 * 1024 * 1024
_ID = re.compile(r'(?:RJ|VJ)\d{6,10}', re.I)
_VN = re.compile(r'v[1-9]\d*', re.I)


class ProviderError(Exception):
    def __init__(self, code, message, retry_after=0):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


def capabilities():
    return {name: dict(search=item.search, details=item.details, cover=item.cover,
                       requires_confirmation=True) for name,item in SOURCES.items()}



def _check_stop(stop):
    if stop and stop():
        raise ProviderError('cancelled', '查询已取消')


def _validate_url(url, source, cover=False):
    if not valid_url(source,url,cover):
        raise ProviderError('unsafe_url','资料地址不属于允许的官方来源')
    return str(url)



def _retry_seconds(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 60


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, source, cover):
        self.source, self.cover = source, cover

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl, self.source, self.cover)
        # A redirect is another upstream request: stop here rather than evade
        # the serialized pacing/robots checks by urllib's automatic redirect.
        raise ProviderError('redirect', '来源返回重定向，请确认官方详情地址后重试')


def _transport(url, method, data, headers, limit, source, cover):
    opener = urllib.request.build_opener(_Redirect(source, cover))
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=TIMEOUT) as response:
            raw = response.read(limit + 1)
            return response.status, dict(response.headers.items()), raw, response.geturl()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), b'', url
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise ProviderError('network', '无法连接资料来源，请检查网络或系统代理') from error


class ProviderClient:
    """An injectable client; one lock and request interval per source."""
    def __init__(self, transport=None, interval=2, clock=time.monotonic, sleep=time.sleep):
        self.transport = transport or _transport
        self.interval, self.clock, self.sleep = max(2, interval), clock, sleep
        self.locks = {name: threading.Lock() for name in capabilities()}
        self.next_at = {name: 0 for name in capabilities()}
        self.cooldown = {}
        self.cache, self.robots = {}, {}
        self.bangumi_token = ""
        self.auth_generation = 0

    def _request(self, source, url, stop, method='GET', data=None,
                 limit=MAX_RESPONSE, cover=False, robots=False):
        _validate_url(url, source, cover)
        _check_stop(stop)
        blocked = self.cooldown.get(source)
        if blocked and blocked[0] > self.clock():
            raise ProviderError(blocked[1], '来源访问限制尚未结束，请稍后重试', blocked[0] - self.clock())
        while self.clock() < self.next_at[source]:
            _check_stop(stop)
            self.sleep(min(.1, self.next_at[source] - self.clock()))
        _check_stop(stop)
        self.next_at[source] = self.clock() + self.interval
        headers = {'User-Agent': UA, 'Accept-Encoding': 'identity',
                   'Accept': 'image/jpeg,image/png,image/webp' if cover else 'application/json,text/html,text/plain'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        if source == 'bangumi' and not cover and urllib.parse.urlsplit(url).hostname == 'api.bgm.tv' and self.bangumi_token:
            headers['Authorization'] = 'Bearer ' + self.bangumi_token
        try:
            status, headers, raw, final = self.transport(url, method, data, headers, limit, source, cover)
        except Exception as exc:
            if source == 'bangumi':
                raise ProviderError('network','Bangumi连接失败，请检查网络或令牌') from None
            raise
        _validate_url(final, source, cover)
        _check_stop(stop)
        lower = {k.lower(): v for k, v in headers.items()}
        if source == 'bangumi' and status == 401:
            raise ProviderError('unauthorized','Bangumi访问令牌失效，请重新授权')
        if source == 'bangumi' and status == 404:
            raise ProviderError('limited','无法确认此条目；它可能未公开、已移除或需要额外Bangumi访问权限')
        if status in (403, 429):
            wait = max(60, _retry_seconds(lower.get('retry-after')))
            self.next_at[source] = max(self.next_at[source], self.clock() + wait)
            self.cooldown[source] = (self.clock() + wait, 'access_denied' if status == 403 else 'rate_limited')
            raise ProviderError('access_denied' if status == 403 else 'rate_limited',
                                '来源拒绝访问' if status == 403 else '来源限流，请稍后重试', wait)
        if robots and status in (404, 410):
            return b'', lower
        if status >= 500:
            raise ProviderError('server_error', '资料来源暂时不可用')
        if status == 404:
            raise ProviderError('not_found', '未找到对应作品')
        if status != 200:
            raise ProviderError('http_error', '资料来源返回HTTP %s' % status)
        if len(raw) > limit:
            raise ProviderError('too_large', '来源响应超过大小限制')
        return raw, lower

    def _allowed(self, source, url, stop, cover=False):
        if source != 'dlsite':
            return
        parsed = urllib.parse.urlsplit(url)
        origin = 'https://' + parsed.hostname
        cached = self.robots.get(origin)
        if not cached or self.clock() - cached[0] >= 600:
            raw, headers = self._request(source, origin + '/robots.txt', stop, cover=cover, robots=True)
            if raw and (headers.get('content-type', '').split(';')[0].lower() not in ('text/plain', '')
                        or raw.lstrip().lower().startswith((b'<html', b'<!doctype'))):
                raise ProviderError('robots_unavailable', '无法确认来源robots规则，已停止查询')
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(raw.decode('utf-8', 'replace').splitlines())
            self.robots[origin] = (self.clock(), parser)
        else:
            parser = cached[1]
        if not parser.can_fetch(UA, url):
            raise ProviderError('robots_denied', '来源robots规则不允许此地址，已停止查询')
        delay = parser.crawl_delay(UA) or 0
        rate = parser.request_rate(UA)
        if rate and rate.requests:
            delay = max(delay, rate.seconds / rate.requests)
        self.next_at[source] = max(self.next_at[source], self.clock() + max(self.interval, delay))

    def _run(self, source, key, stop, callback):
        if source not in self.locks:
            raise ProviderError('unsupported_source', '不支持此资料来源')
        while not self.locks[source].acquire(timeout=.1):
            _check_stop(stop)
        try:
            _check_stop(stop)
            cached = self.cache.get((source, key))
            if cached and self.clock() < cached[0]:
                if isinstance(cached[1], ProviderError):
                    raise ProviderError(cached[1].code, str(cached[1]), cached[1].retry_after)
                return copy.deepcopy(cached[1])
            try:
                value = callback()
            except ProviderError as error:
                if error.code != 'cancelled':
                    self.cache[(source, key)] = (self.clock() + 60, error)
                    if len(self.cache) > 128:
                        self.cache.pop(next(iter(self.cache)))
                raise
            self.cache[(source, key)] = (self.clock() + (600 if value else 60), value)
            # bounded process cache; no paths or queries are persisted.
            if len(self.cache) > 128:
                self.cache.pop(next(iter(self.cache)))
            return copy.deepcopy(value)
        finally:
            self.locks[source].release()

    def search(self, source, query, should_stop=None):
        query = _query(query)
        if source == 'steam':
            raise ProviderError('unsupported_capability','Steam仅支持本机manifest识别和官方入口')
        return self._run(source, ('search', query, self.auth_generation if source == 'bangumi' else 0), should_stop,
                         lambda: self._search(source, query, should_stop))

    def details(self, source, external_id, should_stop=None):
        external_id = str(external_id).strip()
        valid = valid_id(source, external_id)
        if not valid:
            raise ProviderError('invalid_id', '请输入明确的作品编号')
        result = self.search(source, external_id, should_stop)
        if not result:
            raise ProviderError('not_found', '未找到对应作品')
        if len(result) != 1 or result[0]['external_id'].lower() != external_id.lower():
            raise ProviderError('identity_mismatch', '来源作品编号不匹配')
        return result[0]

    def set_bangumi_token(self, token):
        from slg_bangumi_auth import validate_format
        self.bangumi_token = validate_format(token) if token else ''
        self.auth_generation += 1
        self.cache = {key:value for key,value in self.cache.items() if key[0] != 'bangumi'}

    def validate_bangumi_token(self, token, should_stop=None):
        from slg_bangumi_auth import validate_format
        probe=ProviderClient(self.transport, self.interval, self.clock, self.sleep)
        probe.set_bangumi_token(validate_format(token))
        raw,_=probe._request('bangumi','https://api.bgm.tv/v0/me',should_stop)
        try:
            row=json.loads(raw)
            if not isinstance(row,dict) or not isinstance(row.get('id'),int): raise ValueError()
            return {'valid':True}
        except (ValueError,TypeError):
            raise ProviderError('parse_error','Bangumi授权验证响应无效') from None

    def _search(self, source, query, stop):
        if source == 'bangumi':
            return self._bangumi_search(query,stop)
        if source == 'vndb':
            vid = _VN.fullmatch(query)
            if '://' in query:
                _validate_url(query, source)
                vid = _VN.fullmatch(urllib.parse.urlsplit(query).path.strip('/'))
                if not vid:
                    raise ProviderError('invalid_id', 'VNDB链接须指向作品编号')
            filters = ['id', '=', vid[0].lower()] if vid else ['search', '=', query]
            payload = json.dumps({'filters': filters, 'fields': 'title,alttitle,titles{lang,title,latin},aliases,released,image.url,developers.name',
                                  'results': 10, 'sort': 'id' if vid else 'searchrank'}).encode('utf-8')
            raw, _ = self._request(source, 'https://api.vndb.org/kana/vn', stop, 'POST', payload)
            try:
                result = json.loads(raw)['results']
                if not isinstance(result, list):
                    raise ValueError()
                items = []
                for row in result[:10]:
                    if not isinstance(row, dict) or not _VN.fullmatch(str(row.get('id', ''))):
                        raise ValueError()
                    if vid and row['id'].lower() != vid[0].lower():
                        raise ProviderError('identity_mismatch', 'VNDB返回不同作品编号')
                    cover = (row.get('image') or {}).get('url') or ''
                    if cover:
                        _validate_url(cover, source, True)
                    candidate = _candidate(source, row['id'], row.get('title'), row.get('alttitle'),
                        ' / '.join(d.get('name', '') for d in row.get('developers', [])), row.get('released'),
                        'https://vndb.org/' + row['id'], cover, '来源编号' if vid else '来源名称搜索')
                    candidate['aliases'] = _vndb_aliases(row)
                    items.append(candidate)
                return items
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ProviderError('parse_error', 'VNDB响应格式无法识别') from error
        identity = _ID.fullmatch(query)
        if '://' in query:
            _validate_url(query, source)
            identity = re.search(r'/product_id/((?:RJ|VJ)\d{6,10})\.html(?:$|[?])', query, re.I)
            if not identity:
                raise ProviderError('invalid_id', 'DLsite链接须指向官方作品详情')
            external_id = identity[1].upper()
            parsed = urllib.parse.urlsplit(query)
            url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
        elif identity:
            external_id = identity[0].upper()
            section = 'pro' if external_id.startswith('VJ') else 'maniax'
            url = f'https://www.dlsite.com/{section}/work/=/product_id/{external_id}.html'
        else:
            external_id = ''
            url = 'https://www.dlsite.com/maniax/fsr/=/keyword/' + urllib.parse.quote(query, safe='')
        self._allowed(source, url, stop)
        raw, _ = self._request(source, url, stop)
        page = _Page()
        page.feed(raw.decode('utf-8', 'replace'))
        if external_id:
            return [_dl_detail(page, external_id, url)]
        candidates = []
        seen = set()
        for href, title in page.links:
            link = urllib.parse.urljoin(url, href)
            try:
                _validate_url(link, source)
            except ProviderError:
                continue
            match = re.search(r'/product_id/((?:RJ|VJ)\d{6,10})\.html', link, re.I)
            if match and title.strip() and match[1].upper() not in seen:
                seen.add(match[1].upper())
                candidates.append(_candidate(source, match[1].upper(), title.strip(), '', '', '', link, '', '官方名称搜索；详情待查询'))
        if not candidates and not page.has_search_results:
            raise ProviderError('parse_error', 'DLsite搜索页面未返回可识别的结果，请尝试RJ/VJ编号')
        return candidates[:10]

    def _bangumi_search(self, query, stop):
        identity=query if valid_id('bangumi',query) else None
        if '://' in query:
            _validate_url(query,'bangumi')
            match=re.fullmatch(r'/subject/([1-9][0-9]{0,9})/?',urllib.parse.urlsplit(query).path)
            if not match: raise ProviderError('invalid_id','Bangumi链接须指向作品条目')
            identity=match[1]
        if identity:
            raw,_=self._request('bangumi','https://api.bgm.tv/v0/subjects/'+identity,stop)
        else:
            payload=json.dumps({'keyword':query,'sort':'match','filter':{'type':[4],'nsfw':None}}).encode()
            raw,_=self._request('bangumi','https://api.bgm.tv/v0/search/subjects?limit=10&offset=0',stop,'POST',payload)
        try:
            body=json.loads(raw)
            rows=[body] if identity else body['data']
            if not isinstance(rows,list): raise ValueError()
            result=[]
            for row in rows[:10]:
                if not isinstance(row,dict): raise ValueError()
                if row.get('type')!=4:
                    if identity: raise ProviderError('wrong_type','此Bangumi条目不是游戏')
                    continue
                ext=str(row.get('id',''))
                if not valid_id('bangumi',ext): raise ValueError()
                if identity and identity!=ext: raise ProviderError('identity_mismatch','Bangumi返回不同条目')
                title=row.get('name_cn') or row.get('name')
                if not isinstance(title,str) or not title.strip(): raise ValueError()
                cover=(row.get('images') or {}).get('large') or (row.get('images') or {}).get('common') or ''
                if cover: _validate_url(cover,'bangumi',True)
                developer=''
                for item in row.get('infobox') or []:
                    if isinstance(item,dict) and item.get('key') in ('开发','开发商','游戏开发商') and isinstance(item.get('value'),str):
                        developer=item['value'][:512]; break
                candidate=_candidate('bangumi',ext,title,row.get('name') or '',developer,row.get('date') or '',
                                     'https://bgm.tv/subject/'+ext,cover,'Bangumi游戏资料；请确认作品')
                candidate['aliases']=[v for v in (row.get('name'),row.get('name_cn')) if isinstance(v,str) and v][:2]
                candidate['access_limited']=not bool(self.bangumi_token)
                result.append(candidate)
            if not result and not self.bangumi_token:
                raise ProviderError('limited','匿名结果可能不完整；可授权Bangumi后重试')
            return result
        except (ValueError,TypeError,KeyError,AttributeError):
            raise ProviderError('parse_error','Bangumi响应格式无法识别') from None

    def structured_details(self, external_id, should_stop=None):
        """Known DLsite identity via product JSON. Never guesses identities or image URLs."""
        identity = str(external_id).strip().upper()
        if not _ID.fullmatch(identity):
            raise ProviderError('invalid_id', '请输入RJ或VJ编号')
        def fetch():
            section = 'pro' if identity.startswith('VJ') else 'maniax'
            url = f'https://www.dlsite.com/maniax/api/=/product.json?workno={identity}'
            self._allowed('dlsite', url, should_stop)
            raw, headers = self._request('dlsite', url, should_stop)
            try:
                payload = json.loads(raw)
                identity_fields = ('workno', 'product_id', 'work_id')
                if isinstance(payload, dict):
                    row = payload.get(identity)
                    keyed_identity = identity if isinstance(row, dict) else ''
                    if row is None and any(str(payload.get(field, '')).upper() == identity for field in identity_fields):
                        row = payload
                elif isinstance(payload, list):
                    keyed_identity = ''
                    matching = [r for r in payload if isinstance(r, dict) and
                                any(str(r.get(field, '')).upper() == identity for field in identity_fields)]
                    if len(matching) > 1:
                        raise ProviderError('identity_mismatch', '结构化资料返回重复身份，请勿自动采用')
                    row = matching[0] if matching else None
                else:
                    row = None
                    keyed_identity = ''
                if not isinstance(row, dict):
                    raise ValueError()
                returned_ids = [str(row[field]).upper() for field in identity_fields if field in row]
                if keyed_identity:
                    returned_ids.append(keyed_identity)
                if not returned_ids or any(value != identity for value in returned_ids):
                    raise ProviderError('identity_mismatch', '结构化资料编号不一致')
                title = row.get('work_name') or row.get('title')
                if not isinstance(title, str) or not title.strip():
                    raise ValueError()
                image = row.get('image_main') or row.get('work_image') or row.get('image') or ''
                if isinstance(image, dict):
                    image = image.get('url') or image.get('main') or image.get('main_url') or ''
                cover = ''
                if isinstance(image, str) and image:
                    image = urllib.parse.urljoin(url, image)
                    _validate_url(image, 'dlsite', True)
                    if re.search(r'(?<![A-Za-z0-9])' + re.escape(identity) + r'(?![A-Za-z0-9])', image, re.I):
                        cover = image
                developer = row.get('maker_name') or row.get('circle_name') or ''
                release = row.get('release_date') or row.get('regist_date') or ''
                return _candidate('dlsite', identity, title.strip(), title.strip(),
                                  developer if isinstance(developer, str) else '',
                                  release if isinstance(release, str) else '',
                                  f'https://www.dlsite.com/{section}/work/=/product_id/{identity}.html',
                                  cover, '官方结构化资料；编号已核对')
            except (ValueError, TypeError, AttributeError) as exc:
                raise ProviderError('parse_error', 'DLsite结构化资料格式无法识别') from exc
        return self._run('dlsite', ('product_json', identity), should_stop, fetch)

    def search_candidates(self, source, query, should_stop=None):
        """Explicit lookup with release fallback; all results still require selection."""
        query = _query(unicodedata.normalize('NFKC', str(query)))
        if source == 'dlsite':
            identity = _ID.fullmatch(query)
            if '://' in query:
                _validate_url(query, source)
                identity = re.search(r'/product_id/((?:RJ|VJ)\d{6,10})\.html$', urllib.parse.urlsplit(query).path, re.I)
                if not identity:
                    raise ProviderError('invalid_id', 'DLsite链接须指向官方作品详情')
            if identity:
                return [self.structured_details(identity[1] if identity.lastindex else identity[0], should_stop)]
            rows = self.search(source, query, should_stop)
            # Search commercial works independently; retaining all identities is
            # essential when the same title exists in both product sections.
            def commercial_lookup():
                url = 'https://www.dlsite.com/pro/fsr/=/keyword/' + urllib.parse.quote(query, safe='')
                self._allowed(source, url, should_stop)
                raw, _ = self._request(source, url, should_stop)
                page = _Page()
                page.feed(raw.decode('utf-8', 'replace'))
                result, seen = [], set()
                for href, title in page.links:
                    link = urllib.parse.urljoin(url, href)
                    try:
                        _validate_url(link, source)
                    except ProviderError:
                        continue
                    found = re.search(r'/product_id/((?:RJ|VJ)\d{6,10})\.html', link, re.I)
                    if found and title.strip() and found[1].upper() not in seen:
                        seen.add(found[1].upper())
                        result.append(_candidate(source, found[1].upper(), title.strip(), '', '', '', link, '', '官方商业作品名称搜索；请确认'))
                if not result and not page.has_search_results:
                    raise ProviderError('parse_error', 'DLsite商业作品页面无法识别，请尝试VJ编号')
                return result[:10]
            commercial = self._run(source, ('commercial', query), should_stop, commercial_lookup)
            seen = {row['external_id'] for row in rows}
            return (rows + [row for row in commercial if row['external_id'] not in seen])[:10]
        rows = self.search(source, query, should_stop)
        if rows or source != 'vndb' or _VN.fullmatch(query) or '://' in query:
            return rows
        def release_lookup():
            payload = json.dumps({'filters': ['search', '=', query],
                                  'fields': 'id,title,vns.id', 'results': 10,
                                  'sort': 'searchrank'}).encode('utf-8')
            raw, _ = self._request('vndb', 'https://api.vndb.org/kana/release', should_stop, 'POST', payload)
            try:
                releases = json.loads(raw)['results']
                if not isinstance(releases, list):
                    raise ValueError()
                ids = []
                for release in releases[:10]:
                    for vn in release.get('vns', []):
                        vid = vn.get('id')
                        if _VN.fullmatch(str(vid or '')) and vid not in ids:
                            ids.append(vid)
                results = []
                for vid in ids[:5]:
                    _check_stop(should_stop)
                    for candidate in self._search('vndb', vid, should_stop):
                        candidate['match_reason'] = '发行版本搜索；请确认原作品'
                        results.append(candidate)
                return results
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise ProviderError('parse_error', 'VNDB发行版本响应无法识别') from exc
        return self._run('vndb', ('release_fallback', query), should_stop, release_lookup)

    def download_cover(self, cover_url, source, should_stop=None):
        _validate_url(cover_url, source, True)
        def download():
            self._allowed(source, cover_url, should_stop, True)
            raw, headers = self._request(source, cover_url, should_stop, limit=MAX_COVER, cover=True)
            if headers.get('content-type', '').split(';')[0].lower() not in ('image/jpeg', 'image/png', 'image/webp'):
                raise ProviderError('invalid_image', '来源未返回JPEG、PNG或WebP图片')
            from slg_dlsite_online import validate_image
            try:
                validate_image(raw)
            except ValueError as error:
                raise ProviderError('invalid_image', '来源图片损坏或超过尺寸限制') from error
            return raw
        return self._run(source, ('cover', cover_url), should_stop, download)


def _query(value):
    value = str(value).strip()
    if not value or len(value) > 200 or re.search(r'[\x00-\x1f]', value):
        raise ProviderError('invalid_query', '查询词须为1至200个字符')
    if re.match(r'^[A-Za-z]:[\\/]|^\\\\|^/|^file:', value, re.I):
        raise ProviderError('local_path', '请只填写作品名称或编号，不发送本机路径')
    return value


def _candidate(source, identity, title, original, developer, date, url, cover, reason):
    return dict(source=source, external_id=identity, title=title or '', original_title=original or '',
                developer=developer or '', release_date=date or '', source_url=url,
                cover_url=cover, match_reason=reason)


def _vndb_aliases(row):
    """Bounded names for explicit confirmation, not automatic identity matching."""
    values = [row.get('title'), row.get('alttitle')]
    titles = row.get('titles') or []
    if isinstance(titles, list):
        for item in titles[:64]:
            if isinstance(item, dict):
                values.extend((item.get('title'), item.get('latin')))
    aliases = row.get('aliases') or []
    if isinstance(aliases, str):
        values.extend(aliases.splitlines()[:64])
    elif isinstance(aliases, list):
        values.extend(aliases[:64])
    found, seen = [], set()
    for value in values:
        if not isinstance(value, str) or len(value) > 512 or re.search(r'[\x00-\x1f\x7f]', value):
            continue
        value = re.sub(r'\s+', ' ', value).strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            found.append(value)
            if len(found) >= 64:
                break
    return found


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.links, self.images, self.jsonld = {}, [], [], []
        self.anchor, self.text, self.script = None, [], None
        self.has_search_results = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            self.meta[attrs.get('property') or attrs.get('name')] = attrs.get('content', '')
        if tag == 'link' and attrs.get('rel') == 'canonical':
            self.meta['canonical'] = attrs.get('href', '')
        if tag == 'a':
            self.anchor = [attrs.get('href', ''), attrs.get('title', '')]
        if tag == 'img':
            self.images.append(attrs)
            if self.anchor and attrs.get('alt'):
                self.anchor[1] += attrs['alt']
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.script = []
        if 'search_result' in attrs.get('id', '') or 'work_list' in attrs.get('class', ''):
            self.has_search_results = True

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif self.anchor:
            self.anchor[1] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor:
            self.links.append(tuple(self.anchor))
            self.anchor = None
        if tag == 'script' and self.script is not None:
            try:
                self.jsonld.append(json.loads(''.join(self.script)))
            except ValueError:
                pass
            self.script = None


def _dl_detail(page, identity, url):
    canonical = page.meta.get('canonical') or page.meta.get('og:url') or ''
    found = re.search(r'/product_id/((?:RJ|VJ)\d{6,10})\.html', canonical, re.I)
    if not found or found[1].upper() != identity:
        raise ProviderError('identity_mismatch', 'DLsite页面无法确认请求的作品编号')
    _validate_url(canonical, 'dlsite')
    product = {}
    def walk(value):
        nonlocal product
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            kind = value.get('@type', '')
            if kind == 'Product' or isinstance(kind, list) and 'Product' in kind:
                product = value
            if '@graph' in value:
                walk(value['@graph'])
    for item in page.jsonld:
        walk(item)
    sku = str(product.get('sku') or product.get('productID') or '')
    if sku and _ID.fullmatch(sku) and sku.upper() != identity:
        raise ProviderError('identity_mismatch', 'DLsite结构化资料编号与详情地址不一致')
    title = product.get('name') or page.meta.get('og:title') or ''
    if not title:
        raise ProviderError('parse_error', '官方页面没有可读取的作品名称')
    title = re.sub(r'\s*[|｜]\s*DLsite.*$', '', title).strip()
    images = [page.meta.get('og:image', '')]
    structured = product.get('image')
    if isinstance(structured, str):
        images.append(structured)
    elif isinstance(structured, list):
        images.extend(x for x in structured if isinstance(x, str))
    for image in page.images:
        for key in ('data-src', 'src'):
            images.append(image.get(key, ''))
        for entry in (image.get('srcset') or image.get('data-srcset') or '').split(','):
            images.append(entry.strip().split(' ')[0])
    cover = ''
    for value in images:
        value = urllib.parse.urljoin(url, html.unescape(value))
        try:
            _validate_url(value, 'dlsite', True)
        except ProviderError:
            continue
        # Cover path must carry the same product ID; do not guess filename.
        if re.search(r'(?<![A-Za-z0-9])' + identity + r'(?!\d)', value, re.I):
            cover = value
            break
    brand = product.get('brand') or product.get('manufacturer') or ''
    developer = brand.get('name', '') if isinstance(brand, dict) else brand if isinstance(brand, str) else ''
    return _candidate('dlsite', identity, title, title, developer,
                      product.get('releaseDate') or '', canonical, cover, '官方详情编号已核对')


_CLIENT = ProviderClient()
_AUTH_LOADED = False
_AUTH_LOCK = threading.Lock()

def _ensure_local_auth():
    global _AUTH_LOADED
    with _AUTH_LOCK:
        if not _AUTH_LOADED:
            import slg_db
            load_local_bangumi_token(slg_db.app_dir())
            _AUTH_LOADED = True



def search(source, query, should_stop=None):
    if source == 'bangumi': _ensure_local_auth()
    return _CLIENT.search(source, query, should_stop)


def details(source, external_id, should_stop=None):
    if source == 'bangumi': _ensure_local_auth()
    return _CLIENT.details(source, external_id, should_stop)


def download_cover(cover_url, source, should_stop=None):
    if source == 'bangumi': _ensure_local_auth()
    return _CLIENT.download_cover(cover_url, source, should_stop)


def query_variants(*values, limit=6):
    """IDs precede bounded normalized titles; full disk paths are never queries."""
    result, identities, names, seen = [], [], [], set()
    for value in values:
        if isinstance(value, (list, tuple)):
            entries = value[:16]
        else:
            entries = [value]
        for entry in entries:
            if not isinstance(entry, str):
                continue
            entry = unicodedata.normalize('NFKC', entry).strip()
            if re.match(r'^[A-Za-z]:[\\/]|^\\\\|^/|^file:', entry, re.I):
                continue
            identity = _ID.fullmatch(entry) or _VN.fullmatch(entry)
            if identity:
                identities.append(entry.upper() if _ID.fullmatch(entry) else entry.lower())
                continue
            from slg_scan import folder_title
            names.extend((entry, folder_title(entry)))
    for value in identities + names:
        value = re.sub(r'\s+', ' ', value).strip()
        key = value.casefold()
        if value and len(value) <= 200 and key not in seen and not re.search(r'[\x00-\x1f]', value):
            seen.add(key)
            result.append(value)
            if len(result) >= max(1, min(6, limit)):
                break
    return result


def search_candidates(source, query, should_stop=None):
    if source == 'bangumi': _ensure_local_auth()
    return _CLIENT.search_candidates(source, query, should_stop)


def structured_details(external_id, should_stop=None):
    return _CLIENT.structured_details(external_id, should_stop)


def lookup_variants(source, values, should_stop=None):
    """User initiated only. Up to three distinct title variants, stop at candidates.

    An explicit ID never falls back to names, preventing wrong-title association.
    """
    variants = query_variants(values, limit=3)
    if not variants:
        raise ProviderError('invalid_query', '没有可用于查询的作品名称或编号')
    for term in variants:
        _check_stop(should_stop)
        rows = search_candidates(source, term, should_stop)
        if rows or _ID.fullmatch(term) or _VN.fullmatch(term):
            return rows
    return []


def load_local_bangumi_token(app_directory):
    """Explicit startup hook. Missing token is anonymous; corrupt token never leaks."""
    global _AUTH_LOADED
    from pathlib import Path
    from slg_bangumi_auth import TokenStore
    try:
        token=TokenStore(Path(app_directory)/'private-credentials').load()
        _CLIENT.set_bangumi_token(token)
        _AUTH_LOADED = True
        return bool(token)
    except OSError:
        _CLIENT.set_bangumi_token('')
        return False
