"""dikgames.com scraping.

Two kinds of page, each giving different things:

  list page  /tag/<tag>/page/<N>/   ~20 games each, with tags, cover, and a
                                    <a title="Name [vX] [Dev]"> that carries
                                    the version for free
  detail page /<slug>/              the site rating, the full overview, and an
                                    explicit Version: line

The list page is cheap and the detail page is not, so sync walks tags and only
fetches detail pages for games that are missing a rating.

robots.txt is `Disallow:` with a pointer at sitemap_index.xml, so crawling is
explicitly fine. Requests still go out one at a time with a delay - the whole
site is ~1000 games and there is no reason to hammer it.
"""

import gzip
import html
import http.client
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime

import slg_db
import slg_util

BASE = "https://dikgames.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) slgking/0.1 "
      "(personal library tool)")
DELAY = 1.0
TIMEOUT = 30

# The theme registers one thumbnail size and uses it everywhere; the sitemap
# only ever links the full-size original. Cover downloads ask for the sized one
# - 18KB against 177KB, and the original is pure waste at 112x69 on screen.
THUMB_SUFFIX = "-576x356"

# post-sitemap3.xml answers HTTP 500 as of 2026-09-18. The other three carry
# 2066 posts between them. A missing shard costs coverage, not correctness: the
# slugs it holds are old games that the tag walk still backstops.
SITEMAPS = ("post-sitemap.xml", "post-sitemap2.xml", "post-sitemap4.xml")

# A guard, not a policy: the depth walk already stops at the tab's own closing
# tag, so this only trips if the markup is broken. The old 2000 clipped 73 rows
# mid-sentence - the longest real blurb in the db was exactly 2000.
OVERVIEW_MAX = 20000

# The sitemap lists the whole site, and the library holds barely half of it -
# the tag walk only ever visited netorare/corruption/cheating, so ~1200 games
# have never been seen. Ingesting those is a one-off 20-minute job, so it is
# rationed per run and the rest rolls into the next sync.
NEW_PER_RUN = 200

_SECTION = re.compile(r'<section class="gp-post-item[^"]*".*?</section>', re.S)
_TAG_URL = re.compile(r'href="%s/tag/([a-z0-9-]+)/"' % re.escape(BASE))
_PLATFORM = re.compile(r'\bplatform-([a-z0-9-]+)')
_TITLE_LINK = re.compile(
    r'<a href="(https://dikgames\.com/[^"]+)"\s+title="([^"]*)"')
_COVER = re.compile(r'data-src="(https://dikgames\.com/wp-content/uploads/[^"]+)"')
_PUBDATE = re.compile(r'itemprop="datePublished"\s+datetime="([^"]+)"')
_RATING = re.compile(r'"ratingValue":\s*"?([\d.]+)')
# The noun is singular at one: the site writes '1 view', '0 like', '1 Comment'
# and 'No Comments' for the low counts that most of the catalogue sits at. A
# hardcoded plural matched none of those, so the three columns stayed NULL for
# every game that was not already popular - and because the rating comes from
# JSON-LD and always matched, a quiet game showed a score and nothing else.
_VIEWS = re.compile(r'gp-meta-views">([\d,]+)\s+views?<')
_LIKES = re.compile(r'gp-meta-likes">([\d,]+)\s+likes?<')
_COMMENTS = re.compile(r'comments-link"[^>]*>\s*([\d,]+|No)\s+Comments?<', re.I)
_VERSION_LINE = re.compile(r'Version:\s*([^<\n]{0,24})')
_DEV_LINE = re.compile(r'Developer:\s*([^<\n]{0,40})')
# Elementor numbers its tab instances per page, so the id is not a site-wide
# constant: agent17 is -1601, cane-and-able is -4591, xxxfiles is -8471. The
# tab title always carries its content id in aria-controls, so read it there
# instead of guessing.
_OVERVIEW_TAB = re.compile(
    r'aria-controls="elementor-tab-content-(\d+)"[^>]*>\s*Overview\s*<')
_OVERVIEW_DIV = re.compile(r"</?div\b[^>]*>")
_OG_IMAGE = re.compile(r'property="og:image"\s+content="([^"]+)"')
_OG_TITLE = re.compile(r'property="og:title"\s+content="([^"]*)"')
_BRACKET = re.compile(r"\[([^\]]*)\]")
_VERSIONISH = re.compile(r"^(?:v|ver|r|ep|ch|chapter|part|act|season)?\.?\s*\d", re.I)
_STATUS_WORDS = {"final", "complete", "completed", "finished", "full release"}


def http_get(url, timeout=TIMEOUT):
    """One request, entity-decoded, raw bytes back. Raises on failure.

    Module level so the threaded cover pool can use it without going through
    Fetcher, whose whole job is to serialise requests.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if enc == "deflate":
        return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


class Fetcher:
    """One request at a time, with a gap between them."""

    def __init__(self, delay=DELAY, log=None, should_stop=None):
        self.delay = delay
        self.log = log or (lambda *a: None)
        self.count = 0
        self._last = 0.0
        # A callable rather than an Event: the worker already owns the Event,
        # and this side only ever reads it.
        self._stop = should_stop or (lambda: False)

    def stopped(self):
        return bool(self._stop())

    def _wait(self, seconds):
        """Sleep in slices, so a stop does not have to outwait the pause.

        The gap between requests is a second and the backoff before a retry is
        two, but a plain time.sleep() made both uncancellable - and the gaps
        are where a long sync actually spends its wall clock. Checking between
        slices is what makes the cancel button land in a fraction of a second
        instead of at the next request.
        """
        deadline = time.time() + seconds
        while True:
            if self.stopped():
                return False
            left = deadline - time.time()
            if left <= 0:
                return True
            time.sleep(min(left, 0.1))

    def _get_retry_throttled(self, url, binary):
        """Retry a 429/503 with capped exponential backoff until it recovers.

        A long background sync is exactly where the site starts throttling, so
        this is the one request that gets more than its single retry. Bounded
        on both ends: at most five extra tries and a 60s ceiling, so a site
        that is properly down still hands control back to the caller.
        """
        wait = self.delay * 2
        for _ in range(5):
            if not self._wait(wait):
                return None
            try:
                raw = http_get(url)
                self._last = time.time()
                self.count += 1
                return raw if binary else raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 503):
                    wait = min(wait * 2, 60.0)
                    continue
                return None
            except (urllib.error.URLError, OSError, gzip.BadGzipFile,
                    http.client.HTTPException):
                return None
        return None

    def get(self, url, retries=1, binary=False):
        for attempt in range(retries + 1):
            if self.stopped():
                return None
            wait = self.delay - (time.time() - self._last)
            if wait > 0 and not self._wait(wait):
                return None
            try:
                raw = http_get(url)
                self._last = time.time()
                self.count += 1
                return raw if binary else raw.decode("utf-8", "replace")
            # HTTPException is not an OSError, so a truncated chunked response
            # (IncompleteRead) used to escape this and kill the whole sync
            # partway through - one flaky page cost every page after it.
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 503):
                    self.log("  ! %s (%s %s) · 被限流，退避重试"
                             % (url, exc.code, exc.reason))
                    return self._get_retry_throttled(url, binary)
                self.log("  ! %s (%s)" % (url, exc))
                if attempt == retries:
                    return None
                if not self._wait(self.delay * 2):
                    return None
            except (urllib.error.URLError, OSError, gzip.BadGzipFile,
                    http.client.HTTPException) as exc:
                self.log("  ! %s (%s)" % (url, exc))
                if attempt == retries:
                    return None
                if not self._wait(self.delay * 2):
                    return None
        return None


def _strip_style(html):
    """Drop <style> so its CSS cannot be mistaken for markup.

    Leaving <script> alone on purpose - the JSON-LD with ratingValue lives in
    one.
    """
    return re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S | re.I)


def _clean(text):
    """Strip tags and decode entities.

    Entities matter more than they look: several titles carry &#8211; (an
    en-dash), and leaving it encoded breaks the local-folder matcher, which
    compares normalised titles.
    """
    if not text:
        return None
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip() or None


def _parse_int(text):
    """'48,300' -> 48300; a missing or unparseable value is None."""
    if not text:
        return None
    return int(text.replace(",", ""))


def _parse_count(text):
    """Like _parse_int, but the site's word for zero reads as zero.

    A count element that is present and says 'No Comments' is a real
    0, and storing NULL for it would be wrong in the one place it matters:
    the metrics backfill would re-fetch the page forever. Kept separate from
    _parse_int, whose None means 'the element was not there at all' - that
    distinction is what lets a genuine layout change stay visible as a gap
    instead of being quietly recorded as zero.
    """
    if text and text.strip().lower() == "no":
        return 0
    return _parse_int(text)


_clean_title = slg_db.clean_site_title


def _extract_overview(page):
    """The body of the detail page's Overview tab.

    Two things the old one-shot regex got wrong. It hardcoded the tab id, so
    it only matched the minority of pages whose Elementor instance happened to
    be 1601 - the rest came back NULL and were re-fetched on every sync,
    forever. And it ended the capture at the first "</div></div>", which cuts
    a blurb short as soon as the markup nests a div. Closing by div depth
    costs one pass and fixes both.
    """
    tab = _OVERVIEW_TAB.search(page)
    if not tab:
        return None
    open_at = page.find('id="elementor-tab-content-%s"' % tab.group(1))
    if open_at < 0:
        return None
    start = page.find(">", open_at) + 1
    depth = 1
    for tag in _OVERVIEW_DIV.finditer(page, start):
        depth += -1 if tag.group(0).startswith("</") else 1
        if depth == 0:
            return page[start:tag.start()]
    return None


def parse_title(raw):
    """'Name [v1.4.2 Beta][Final] [aura-dev]' -> name, version, developer, complete.

    Not a fixed two-bracket split: the site glues the status bracket straight
    onto the version one with no space ('[v1.4.2 Beta][Final]'), and some
    entries carry malformed brackets ('[Ep.7 Free]]'). The last group is
    reliably the developer, so the version is whichever earlier group actually
    reads like one.

    A lone bracket is the exception - with nothing after it, 'the last group is
    the developer' would file 'Game [v1.0]' as written by v1.0. Read that one by
    its shape instead.
    """
    # Titles reach here straight from the title attribute and still carry
    # entities ('Horton Bay Stories &#8211; Jake'); the name column and the
    # local-folder matcher both need them decoded.
    raw = html.unescape(raw or "")
    raw = re.sub(r"[\xa0\u200b]", " ", raw).strip()
    if not raw:
        return raw, None, None, 0

    groups = [g.strip() for g in _BRACKET.findall(raw)]
    name = _BRACKET.sub(" ", raw)
    name = re.sub(r"[\[\]]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()

    if len(groups) == 1 and _VERSIONISH.match(groups[0]):
        developer, body = None, groups
    else:
        developer, body = (groups[-1] if groups else None), groups[:-1]
    version = next((g for g in body if _VERSIONISH.match(g)), None)
    complete = any(g.lower() in _STATUS_WORDS for g in body)
    if version:
        version = re.sub(r"^v(?:er)?\.?\s*", "", version, flags=re.I).strip() or None
    return name or raw, version, developer or None, int(complete)


def parse_list_page(page, log=None):
    """Every game on one listing page.

    The parameter is 'page', not 'html' - naming it 'html' shadows the html
    module, and html.unescape() then dies with "str has no attribute
    unescape". parse_title does the decoding, so there is nothing to unescape
    here.
    """
    page = _strip_style(page)
    out = []
    dropped = 0
    for section in _SECTION.findall(page):
        link = _TITLE_LINK.search(section)
        if not link:
            # _TITLE_LINK wants href immediately followed by title; a card whose
            # attributes got reordered is invisible to it and used to vanish with
            # no trace. Counting it makes a markup change on the site show up as
            # a number instead of silently missing games.
            dropped += 1
            continue
        url, raw_title = link.group(1), link.group(2)
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        name, version, developer, complete = parse_title(raw_title)
        name = _clean_title(name)

        classes = re.search(r'<section class="([^"]*)"', section)
        classes = classes.group(1) if classes else ""
        tags = sorted(set(_TAG_URL.findall(section))
                      or set(re.findall(r"\btag-([a-z0-9-]+)", classes)))
        platform = _PLATFORM.search(classes)
        published = _PUBDATE.search(section)
        cover = _COVER.search(section)

        out.append({
            "slug": slug, "url": url, "title": name, "version": version,
            "developer": developer, "tags": tags, "complete": complete,
            "engine": platform.group(1) if platform else None,
            "last_updated": published.group(1)[:10] if published else None,
            "cover_url": cover.group(1) if cover else None,
        })
    if dropped and log is not None:
        log("  ! %d 个卡片未解析（站点的属性顺序可能变了）" % dropped)
    return out


def parse_detail_page(page):
    """Everything one detail page knows, which turns out to be everything.

    Measured on /the-copycat/: 26 tag links, og:image matching the listing
    page's thumbnail, ratingValue 7.3, 'Version: v1.3.0', 'Developer: ...',
    datePublished and the overview. That is the whole row, so an incremental
    sync never has to touch a listing page at all.
    """
    page = _strip_style(page)
    rating = _RATING.search(page)
    version = _VERSION_LINE.search(page)
    developer = _DEV_LINE.search(page)
    overview = _clean(_extract_overview(page))
    cover = _OG_IMAGE.search(page)
    title = _OG_TITLE.search(page)
    published = _PUBDATE.search(page)
    views = _VIEWS.search(page)
    likes = _LIKES.search(page)
    comments = _COMMENTS.search(page)
    return {
        "rating": float(rating.group(1)) if rating else None,
        "version": (_clean(version.group(1)) or "").lstrip("vV") or None
        if version else None,
        "developer": _clean(developer.group(1)) if developer else None,
        "overview": overview[:OVERVIEW_MAX] if overview else None,
        "tags": sorted(set(_TAG_URL.findall(page))),
        "cover_url": _sized_cover(cover.group(1)) if cover else None,
        "title": _clean_title(_clean(title.group(1))) if title else None,
        "last_updated": published.group(1)[:10] if published else None,
        "site_views": _parse_count(views.group(1)) if views else None,
        "site_likes": _parse_count(likes.group(1)) if likes else None,
        "site_comments": _parse_count(comments.group(1)) if comments else None,
    }


def _ext_from_url(url):
    """The file extension of a cover URL, defaulting to .jpg."""
    return os.path.splitext(url.split("?")[0])[1] or ".jpg"


def _sized_cover(url):
    """Swap a full-size upload for the theme's 576x356 crop.

    Only mainscreen* files have the crop registered; the sitemap's other
    images are already 768x432 and stay as they are.
    """
    if not url:
        return url
    stem, ext = os.path.splitext(url)
    if stem.rsplit("/", 1)[-1].startswith("mainscreen") and not stem.endswith(THUMB_SUFFIX):
        return stem + THUMB_SUFFIX + ext
    return url


_URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.S)


def fetch_sitemap(fetcher, log=print):
    """{slug: {'lastmod': date, 'cover': url}} for the whole site, 3 requests.

    Way cheaper than walking tag listings, which visited 2859 game slots for
    1274 games (2.24x) and re-read ~600 pages per re-sync for nothing.
    """
    out = {}
    for name in SITEMAPS:
        xml = fetcher.get("%s/%s" % (BASE, name))
        if not xml:
            log("  ! %s 取不到，跳过" % name)
            continue
        for block in _URL_BLOCK.findall(xml):
            loc = re.search(r"<loc>([^<]+)</loc>", block)
            if not loc:
                continue
            slug = loc.group(1).rstrip("/").rsplit("/", 1)[-1]
            if not slug:
                continue
            mod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
            img = re.search(r"<image:loc>([^<]+)</image:loc>", block)
            out[slug] = {
                "lastmod": mod.group(1)[:10] if mod else None,
                "cover": _sized_cover(img.group(1)) if img else None,
            }
    return out


def tag_names(fetcher):
    """All 130 tag slugs, from the Yoast tag sitemap."""
    xml = fetcher.get("%s/post_tag-sitemap.xml" % BASE)
    if not xml:
        return []
    return sorted({u.rstrip("/").rsplit("/", 1)[-1]
                   for u in re.findall(r"<loc>([^<]+)</loc>", xml)})


def walk_tag(fetcher, tag, max_pages=None, on_progress=None):
    """Yield every game under one tag, page by page, stopping when a page is empty."""
    page, seen = 1, 0
    while True:
        url = ("%s/tag/%s/" % (BASE, tag) if page == 1
               else "%s/tag/%s/page/%d/" % (BASE, tag, page))
        html = fetcher.get(url)
        if not html:
            break
        games = parse_list_page(html, log=fetcher.log)
        if not games:
            break
        yield games
        seen += len(games)
        if on_progress:
            on_progress(tag, page, seen)
        if max_pages and page >= max_pages:
            break
        page += 1


def sync_tags(conn, fetcher, tags, max_pages=None, on_progress=None, log=print,
              should_stop=None):
    """Walk tag listings and upsert. Returns a summary dict."""
    seen, created = set(), 0
    for tag in tags:
        if should_stop and should_stop():
            log("  已停止，剩下的标签下次再来")
            break
        log("抓取标签 %s ..." % tag)
        page_no = 0
        for page_no, games in enumerate(walk_tag(fetcher, tag, max_pages, on_progress), 1):
            for game in games:
                cover_url = game.pop("cover_url", None)
                game_id, is_new = slg_db.upsert_game(conn, **game)
                if is_new:
                    created += 1
                if cover_url:
                    _note_cover(conn, game_id, cover_url)
                seen.add(game_id)
            conn.commit()
            log("  page %d · 累计 %d 款" % (page_no, len(seen)))
            if should_stop and should_stop():
                break
        slg_db.log_sync(conn, tag, page_no, len(seen), created)
    return {"games": len(seen), "new": created, "requests": fetcher.count}


def _note_cover(conn, game_id, url):
    """Record the cover URL without downloading it.

    Still used by the tag walk and as the fallback when an inline download
    fails: a NULL cover_file is not a gap as far as data_gaps is concerned, so
    a URL that is merely dropped would be invisible to 「下载封面」 forever.
    The pending: marker is what keeps it findable.
    """
    row = conn.execute("SELECT cover_file FROM games WHERE id = ?", (game_id,)).fetchone()
    if row and row["cover_file"]:
        return
    conn.execute("UPDATE games SET cover_file = ? WHERE id = ?",
                 ("pending:" + url, game_id))


def _needs_cover(conn, game_id):
    """True when the row holds no real picture yet.

    Empty and 'pending:<url>' both count: the first is a game whose cover was
    never noted, the second one whose thumbnail was noted but never fetched.
    """
    row = conn.execute("SELECT cover_file FROM games WHERE id = ?",
                       (game_id,)).fetchone()
    if row is None or not row["cover_file"]:
        return True
    return row["cover_file"].startswith("pending:")


def _fetch_cover(fetcher, conn, game_id, slug, url, log):
    """Download one cover right now and point the row at it. True if it landed.

    Through the Fetcher rather than http_get, so the picture queues behind the
    same one-request-a-second gate as the pages: this is the second request of
    the game's turn, not a burst beside it.

    A dead image is not an error. The caller notes the URL as pending instead,
    which is both what keeps the row visible to download_covers and what makes
    the failed half retryable without refetching the page.
    """
    blob = fetcher.get(url, binary=True)
    if not blob:
        return False
    ext = _ext_from_url(url)
    name = slug + ext
    try:
        with open(os.path.join(slg_db.covers_dir(), name), "wb") as fh:
            fh.write(blob)
    except OSError as exc:
        log("  ! 写入失败 %s：%s" % (name, exc))
        return False
    slg_db.set_cover(conn, game_id, name)
    return True


def sync_incremental(conn, fetcher, new_limit=None, since=None,
                     on_progress=None, log=print, should_stop=None):
    """Update the catalogue from the sitemaps instead of walking tags.

    Three sitemap requests name every game and the date it last changed, so a
    detail page is fetched only when the game is new or its <lastmod> moved.
    Once the backlog is ingested that is normally zero to five pages a run.
    The tag walk this replaces issued ~100 requests every run, revisited 2.24x
    as many game slots as there are games, and re-read ~600 pages that could
    not possibly contain anything new.

    A synced game arrives with its thumbnail. The detail page names the cover
    in the same response as the blurb, so fetching it here costs one extra
    request inside the game's own turn rather than a separate pass the user has
    to remember to start - and because both halves are committed together, a
    run they stop partway leaves no game with a blurb and no picture.

    A NULL lastmod means the row predates this column, so it adopts the
    current value rather than counting as changed: adopting costs one UPDATE,
    while treating it as changed would mean 1274 detail fetches to learn
    nothing.

    new_limit, when set, rations a single run. None means "ingest the whole
    backlog in one sitting" - what a user who wants to leave the sync running
    in the background asks for. The stop button still lands mid-run; the
    remainder is simply still there next time.

    since, if given as 'YYYY-MM-DD', drops site entries older than that - but
    only entries the library has never held. Games already in the database are
    checked for changes regardless of age, because "old" says nothing about
    whether the author pushed a fix last week. The point is the routine sync:
    with ~1200 never-ingested back catalogue entries in the sitemap, a plain
    run spends its whole new_limit on 2019 games and reports a pile of "new"
    that the user does not care about. Pass since=None for the backfill run
    that deliberately takes them.
    """
    index = fetch_sitemap(fetcher, log=log)
    if should_stop and should_stop():
        # Distinguished from "the sitemaps are unreachable": a stop mid-fetch
        # leaves a partial index, and reporting it as a failed sync would send
        # the user looking for a network problem they do not have.
        log("  已停止")
        return {"catalogue": len(index), "new": 0, "changed": 0, "deferred": 0,
                "skipped_old": 0, "covers": 0, "requests": fetcher.count}
    if not index:
        log("  ! sitemap 全取不到，本次不同步")
        return {"catalogue": 0, "new": 0, "changed": 0, "deferred": 0,
                "skipped_old": 0, "covers": 0, "requests": fetcher.count}

    known = {row["slug"]: row for row in conn.execute(
        "SELECT id, slug, lastmod FROM games")}
    todo, adopted, skipped_old = [], 0, 0
    for slug, meta in sorted(index.items()):
        row = known.get(slug)
        if row is None:
            if since and meta["lastmod"] and meta["lastmod"] < since:
                skipped_old += 1
                continue
            todo.append((slug, meta, True))
        elif row["lastmod"] is None:
            slg_db.set_lastmod(conn, row["id"], meta["lastmod"])
            adopted += 1
        elif meta["lastmod"] and meta["lastmod"] != row["lastmod"]:
            todo.append((slug, meta, False))
    conn.commit()
    # Changed first: there are only ever a handful, and they are the ones the
    # user's own library is already tracking. New games wait their turn.
    changed_todo = [item for item in todo if not item[2]]
    new_todo = [item for item in todo if item[2]]
    if new_limit is None:
        deferred = 0
        todo = changed_todo + new_todo
    else:
        deferred = max(0, len(new_todo) - new_limit)
        todo = changed_todo + new_todo[:new_limit]
    changed = len(changed_todo)
    log("sitemap %d 款 · 首次登记 %d · 本次抓 %d（变动 %d / 新增 %d，余 %d 款留到下次）"
        % (len(index), adopted, len(todo), changed, len(todo) - changed, deferred))
    if skipped_old:
        log("  跳过 %d 款早于 %s 的老游戏（点「补齐历史」可以收录）"
            % (skipped_old, since))

    created, covers = 0, 0
    for i, (slug, meta, is_new) in enumerate(todo, 1):
        if should_stop and should_stop():
            log("  已停止 · 抓到第 %d/%d 款，剩下的下次接着来"
                % (i - 1, len(todo)))
            break
        url = "%s/%s/" % (BASE, slug)
        page = fetcher.get(url)
        if not page:
            # fetcher.get returns None both for a dead page and for a stop.
            # Without this the loop would keep spinning through every remaining
            # slug at one request per second, doing nothing, after the user
            # already asked it to quit.
            if should_stop and should_stop():
                break
            continue
        detail = parse_detail_page(page)
        title = detail["title"] or slug.replace("-", " ").title()
        cover = detail["cover_url"] or meta.get("cover")
        try:
            slg_db.upsert_game(
                conn, slug=slug, url=url, title=title,
                version=detail["version"],
                # The listing page's bracket is the studio; the detail page's
                # Developer: line is often the individual. Neither is wrong,
                # so an existing row keeps whichever it already had.
                developer=None if not is_new else detail["developer"],
                tags=detail["tags"],
                last_updated=detail["last_updated"] or meta["lastmod"],
                lastmod=meta["lastmod"])
            game_id = conn.execute("SELECT id FROM games WHERE slug = ?",
                                   (slug,)).fetchone()["id"]
            # The picture is fetched before upsert_detail, not after it, and
            # that ordering is the whole trick: upsert_detail commits (it has to
            # - the version guard reads back the row it just wrote), so a cover
            # staged behind it could no longer be unwound. Cover first means the
            # two stop checks below can still take the game back whole, which is
            # what "截止到这款之前" has to mean: a game that needs a cover is
            # either committed complete or not committed at all.
            if cover and _needs_cover(conn, game_id):
                if should_stop and should_stop():
                    conn.rollback()
                    log("  已停止 · 抓到第 %d/%d 款，剩下的下次接着来"
                        % (i - 1, len(todo)))
                    break
                if _fetch_cover(fetcher, conn, game_id, slug, cover, log):
                    covers += 1
                elif should_stop and should_stop():
                    # The stop landed on the picture request itself. Same
                    # unwind: nothing about this game is written down.
                    conn.rollback()
                    log("  已停止 · 抓到第 %d/%d 款，剩下的下次接着来"
                        % (i - 1, len(todo)))
                    break
                else:
                    _note_cover(conn, game_id, cover)
            slg_db.upsert_detail(conn, game_id, overview=detail["overview"],
                                 rating=detail["rating"],
                                 site_views=detail["site_views"],
                                 site_likes=detail["site_likes"],
                                 site_comments=detail["site_comments"])
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run
            conn.rollback()
            log("  ! %s 入库失败：%s" % (slug, exc))
            continue
        created += int(is_new)
        if on_progress:
            on_progress(i, len(todo), title)

    slg_db.log_sync(conn, "incremental", 1, len(index), created)
    return {"catalogue": len(index), "new": created, "changed": changed,
            "deferred": deferred, "skipped_old": skipped_old,
            "covers": covers, "requests": fetcher.count}


def download_covers(conn, limit=None, workers=3, rate=3.0, log=print,
                    on_progress=None, should_stop=None):
    """Fetch the pending thumbnails into data/covers/. Resumable, stoppable.

    A URL is recorded as 'pending:<url>' the moment it is known, so an
    interrupted run needs no progress file: whatever is still pending is
    exactly what is left to do.

    The downloads sit on a few threads and the writes stay on this one. sqlite
    connections cannot cross threads, and the wait is all network - the point
    of the threads is to overlap that wait, not to write faster.
    """
    rows = conn.execute(
        "SELECT id, slug, cover_file FROM games"
        " WHERE cover_file LIKE 'pending:%'"
        + (" LIMIT %d" % int(limit) if limit else "")).fetchall()
    total = len(rows)
    if not total:
        log("没有待下载的封面")
        return 0

    dest = slg_db.covers_dir()
    work = queue.Queue()
    for row in rows:
        work.put(row)
    results = queue.Queue()

    gate = threading.Lock()
    next_at = [0.0]

    def worker():
        while not (should_stop and should_stop()):
            try:
                row = work.get_nowait()
            except queue.Empty:
                return
            # A shared gate, not a per-thread sleep: three threads each
            # sleeping 0.3s would still put ten requests a second on the wire.
            with gate:
                now = time.time()
                wait = next_at[0] - now
                next_at[0] = max(now, next_at[0]) + 1.0 / max(rate, 0.1)
            if wait > 0:
                time.sleep(wait)
            url = row["cover_file"][len("pending:"):]
            blob = None
            for _attempt in range(2):
                try:
                    blob = http_get(url)
                    break
                except Exception:  # noqa: BLE001 - a dead image must not stop the run
                    blob = None
            results.put((row["id"], row["slug"], url, blob))
        results.put((None, None, None, None))  # told to stop: unblock the writer

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()

    done, received, sentinels = 0, 0, 0
    while received < total:
        row_id, slug, url, blob = results.get()
        if row_id is None:
            sentinels += 1
            if sentinels >= len(threads):
                break  # every worker stood down; the rest stays pending
            continue
        received += 1
        if blob:
            ext = _ext_from_url(url)
            name = slug + ext
            try:
                with open(os.path.join(dest, name), "wb") as fh:
                    fh.write(blob)
                slg_db.set_cover(conn, row_id, name)
                conn.commit()
                done += 1
            except OSError as exc:
                log("  ! 写入失败 %s：%s" % (name, exc))
        else:
            log("  ! 下载失败 %s" % url)
        if on_progress:
            on_progress(done, total)
    log("封面下载 %d/%d" % (done, total))
    # The gap count walks the covers directory, and it is memoised because
    # _render_stats asks for it on every refresh. Files just appeared.
    slg_db.invalidate_cover_gaps()
    return done


def enrich(conn, fetcher, limit=200, log=print, on_progress=None,
           should_stop=None):
    """Fill in rating, overview, tags and cover for games that lack them.

    Capped per run so a first sync is not a 20-minute wall - run it again and
    it picks up where it stopped. Pass limit=None to drain the whole backlog
    in one sitting.

    The detail page is a superset of the listing page, so this takes the tags
    and the og:image too. The thumbnail is fetched here as well as in the
    sitemap pass, for the same reason: the URLs were sitting in the db the
    whole time, and the pages that carry them are the same ones being fetched
    for the ratings anyway. A game is only committed once both halves are in.
    """
    rows = conn.execute(
        "SELECT id, slug, url FROM games"
        " WHERE url IS NOT NULL AND fetch_failures < 3"
        "       AND (rating IS NULL OR overview IS NULL OR cover_file IS NULL"
        "            OR site_views IS NULL)"
        # A missing cover is the only gap the user can see, so it outranks a
        # missing rating. Plain last_updated DESC starved the last 13 coverless
        # games: they are old, and the missing-rating backlog is ~980 rows.
        # fetch_failures ASC drops a page that failed last run below a healthy
        # one, and fetch_failures < 3 parks a permanently-dead page so it stops
        # re-consuming a slot every run.
        " ORDER BY (cover_file IS NULL) DESC, fetch_failures ASC, last_updated DESC"
        " LIMIT ?",
        (-1 if limit is None else limit,)).fetchall()
    done, covers = 0, 0
    for row in rows:
        if should_stop and should_stop():
            break
        page = fetcher.get(row["url"])
        if not page:
            slg_db.note_fetch_failure(conn, row["id"])
            conn.commit()
            continue
        slg_db.clear_fetch_failures(conn, row["id"])
        detail = parse_detail_page(page)
        slg_db.upsert_detail(
            conn, row["id"], rating=detail["rating"], version=detail["version"],
            developer=detail["developer"], overview=detail["overview"],
            site_views=detail["site_views"], site_likes=detail["site_likes"],
            site_comments=detail["site_comments"])
        if detail["tags"]:
            slg_db.set_tags(conn, row["id"], detail["tags"])
        # Same as the sitemap pass: the picture is this game's second request,
        # so a stop landing on it unwinds the row and leaves the whole game for
        # the next run rather than committing a blurb with no thumbnail.
        if detail["cover_url"] and _needs_cover(conn, row["id"]):
            if should_stop and should_stop():
                conn.rollback()
                break
            if _fetch_cover(fetcher, conn, row["id"], row["slug"],
                            detail["cover_url"], log):
                covers += 1
            elif should_stop and should_stop():
                conn.rollback()
                break
            else:
                _note_cover(conn, row["id"], detail["cover_url"])
        conn.commit()
        done += 1
        if on_progress:
            on_progress(done, len(rows), row["url"])
    log("详情补全 %d/%d" % (done, len(rows)))
    return {"filled": done, "covers": covers}


def backfill_metrics(conn, fetcher, limit=500, log=print, on_progress=None,
                     should_stop=None):
    """Re-read detail pages for games whose popularity counts are missing.

    Separate from enrich rather than a flag on it: the WHERE differs, this
    needs one request per game where enrich needs two (it also fetches the
    cover), it writes three columns and nothing else, and enrich's
    rollback-on-stop cover unwind has no meaning here. Threading a
    metrics_only flag through enrich would put a conditional in each of those.

    Counts already in the row are not a reason to skip a game - a page can
    carry views but not likes. Only the row's own gaps decide the queue, and
    _metrics_gap_sql is the single definition of what a gap is, shared with
    the number the maintenance dialog shows.

    A game that parses to no counts at all is counted and reported rather
    than silently seeded with zeros: absent metrics are the symptom the
    regex fix was for, and inventing a 0 would hide the next layout change
    exactly the way the old plural-only patterns hid this one.
    """
    rows = slg_db.metrics_gap_rows(conn, limit)
    done, missing = 0, 0
    for row in rows:
        if should_stop and should_stop():
            break
        page = fetcher.get(row["url"])
        if not page:
            slg_db.note_fetch_failure(conn, row["id"])
            conn.commit()
            continue
        slg_db.clear_fetch_failures(conn, row["id"])
        detail = parse_detail_page(page)
        if (detail["site_views"] is None and detail["site_likes"] is None
                and detail["site_comments"] is None):
            missing += 1
            log("  ! %s 三项指标都没解析到（站点结构可能变了）" % row["slug"])
        slg_db.upsert_detail(
            conn, row["id"], site_views=detail["site_views"],
            site_likes=detail["site_likes"],
            site_comments=detail["site_comments"])
        conn.commit()
        done += 1
        if on_progress:
            on_progress(done, len(rows), row["url"])
    remaining = slg_db.metrics_gap_count(conn)
    log("热度指标补全 %d/%d · 未解析到 %d 款 · 还剩 %d 款"
        % (done, len(rows), missing, remaining))
    return {"filled": done, "missing": missing, "remaining": remaining}


# --- CLI -----------------------------------------------------------------------

def _main(argv=None):
    # The console is cp936 here and every line below is Chinese. slg_main sets
    # this for the packaged entry point; running this file directly needs it
    # too.
    slg_util.fix_console()

    import argparse
    parser = argparse.ArgumentParser(prog="slgking scrape", description="抓取 dikgames")
    parser.add_argument("--tag", action="append", default=[],
                        help="要抓的标签（可重复）")
    parser.add_argument("--incremental", action="store_true",
                        help="走 sitemap 增量同步（推荐，约 3 个请求）")
    parser.add_argument("--sitemap", action="store_true",
                        help="只打印 sitemap 索引结果，不写库")
    parser.add_argument("--new-limit", type=int, default=NEW_PER_RUN, metavar="N",
                        help="单次增量最多纳入多少款新游戏（默认 %d）" % NEW_PER_RUN)
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="增量同步只收录这个日期之后的新游戏，"
                             "更老的留到「补齐历史」时再收")
    parser.add_argument("--all-history", action="store_true",
                        help="忽略 --since，把老游戏也一起收录（补齐历史）")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--enrich", type=int, default=0, metavar="N",
                        help="额外补全 N 款游戏的详情（评分/简介）")
    parser.add_argument("--metrics", type=int, default=0, metavar="N",
                        help="为 N 款缺数据的游戏重抓浏览/点赞/评论")
    parser.add_argument("--covers", type=int, default=0, metavar="N",
                        help="额外下载 N 张封面")
    parser.add_argument("--list-tags", action="store_true", help="列出全部标签")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    parser.add_argument("--delay", type=float, default=DELAY)
    args = parser.parse_args(argv)

    fetcher = Fetcher(delay=args.delay, log=print)

    if args.list_tags:
        names = tag_names(fetcher)
        print("共 %d 个标签：" % len(names))
        for name in names:
            print(" ", name)
        return 0

    if args.dry_run:
        tags = args.tag or ["netorare"]
        total, slugs = 0, []
        for tag in tags:
            for games in walk_tag(fetcher, tag, args.max_pages):
                total += len(games)
                for g in games[:2]:
                    slugs.append((tag, g))
        print("dry-run：%d 款（%d 次请求）" % (total, fetcher.count))
        for tag, g in slugs[:6]:
            print("  [%s] %s v%s · %s" % (tag, g["title"], g["version"],
                                          ",".join(g["tags"][:5])))
        return 0

    if args.sitemap:
        index = fetch_sitemap(fetcher, log=print)
        print("sitemap：%d 款（%d 个请求）" % (len(index), fetcher.count))
        for slug in sorted(index)[:6]:
            print("  %s  %s  %s" % (slug, index[slug]["lastmod"],
                                    index[slug]["cover"]))
        return 0

    conn = slg_db.connect()
    try:
        if args.incremental:
            since = None if args.all_history else args.since
            summary = sync_incremental(conn, fetcher, new_limit=args.new_limit,
                                       since=since, log=print)
            print("全站 %d 款 · 新增 %d · 变动 %d · 余 %d 款 · 跳过老游戏 %d 款"
                  " · %d 个请求"
                  % (summary["catalogue"], summary["new"], summary["changed"],
                     summary["deferred"], summary["skipped_old"],
                     summary["requests"]))
        if args.tag:
            summary = sync_tags(conn, fetcher, args.tag, args.max_pages)
            print("入库 %d 款，新增 %d 款" % (summary["games"], summary["new"]))
        if args.covers:
            download_covers(conn, limit=args.covers, log=print)
        if args.enrich:
            enrich(conn, fetcher, args.enrich)
        if args.metrics:
            backfill_metrics(conn, fetcher, args.metrics)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
