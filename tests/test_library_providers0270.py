import io
import json
import threading
import unittest

from PIL import Image
import slg_library_providers as p


class Providers(unittest.TestCase):
    def client(self, replies):
        calls, clock = [], [0.0]
        def transport(url, method, data, headers, limit, source, cover):
            calls.append((url, method, data, headers, limit))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            status, body, content = reply
            return status, {'Content-Type': content}, body, url
        client = p.ProviderClient(transport, clock=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0]+n))
        return client, calls, clock

    def test_vndb_id_and_cache(self):
        raw = json.dumps({'results': [{'id': 'v17', 'title': '名称', 'alttitle': None,
            'released': None, 'developers': [{'name': '制作组'}], 'image': {'url': 'https://t.vndb.org/cv/01/1.jpg'}}]}).encode()
        c, calls, _ = self.client([(200, raw, 'application/json')])
        row = c.details('vndb', 'v17')
        self.assertEqual(row['release_date'], '')
        self.assertEqual(row['developer'], '制作组')
        self.assertEqual(json.loads(calls[0][2])['filters'], ['id', '=', 'v17'])
        row['title'] = 'mutated'
        self.assertEqual(c.details('vndb', 'v17')['title'], '名称')
        self.assertEqual(len(calls), 1)

    def test_vndb_name_not_first_choice(self):
        raw = json.dumps({'results': [{'id': 'v1', 'title': 'one'}, {'id': 'v2', 'title': 'two'}]}).encode()
        c, calls, _ = self.client([(200, raw, 'application/json')])
        self.assertEqual(len(c.search('vndb', '游戏')), 2)
        self.assertEqual(json.loads(calls[0][2])['results'], 10)

    def test_vndb_multilingual_aliases_are_bounded_and_do_not_select_first(self):
        row = {'id': 'v27519', 'title': 'Dohna Dohna', 'alttitle': 'ドーナドーナ',
               'titles': [{'lang': 'zh-Hans', 'title': '多娜多娜 一起干坏事吧', 'latin': None},
                          {'lang': 'ja', 'title': 'ドーナドーナ', 'latin': 'Dohna Dohna'}],
               'aliases': ['多娜多娜', '多娜多娜', 'bad\x00value', 'x' * 513, 9]}
        c, calls, _ = self.client([(200, json.dumps({'results': [row, {'id': 'v2', 'title': 'another'}]}).encode(), 'application/json')])
        result = c.search('vndb', '多娜多娜')
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['aliases'], ['Dohna Dohna', 'ドーナドーナ', '多娜多娜 一起干坏事吧', '多娜多娜'])
        self.assertIn('titles{lang,title,latin}', json.loads(calls[0][2])['fields'])
        self.assertIn('aliases', json.loads(calls[0][2])['fields'])
        result[0]['aliases'].clear()
        self.assertTrue(c.search('vndb', '多娜多娜')[0]['aliases'])

    def test_vndb_legacy_string_aliases_and_large_list(self):
        self.assertEqual(p._vndb_aliases({'aliases': 'Alpha\nBeta\nAlpha'}), ['Alpha', 'Beta'])
        self.assertEqual(len(p._vndb_aliases({'aliases': [str(i) for i in range(500)]})), 64)

    def test_vndb_different_identity(self):
        c, _, _ = self.client([(200, b'{"results":[{"id":"v2","title":"x"}]}', 'application/json')])
        with self.assertRaises(p.ProviderError) as cm:
            c.details('vndb', 'v1')
        self.assertEqual(cm.exception.code, 'identity_mismatch')

    def detail(self, identity='RJ123456', image='https://img.dlsite.jp/mod/RJ123456_img_main.jpg'):
        return ('<link rel="canonical" href="https://www.dlsite.com/maniax/work/=/product_id/%s.html">'
                '<meta property="og:title" content="作品 | DLsite"><img data-srcset="%s 2x">'
                '<script type="application/ld+json">{"@type":"Product","name":"作品","brand":{"name":"社团"}}</script>'
                % (identity, image)).encode()

    def test_dlsite_robots_lazy_image(self):
        c, calls, clock = self.client([(200, b'User-agent: *\nAllow: /\nCrawl-delay: 4', 'text/plain'),
                                     (200, self.detail(), 'text/html')])
        row = c.details('dlsite', 'RJ123456')
        self.assertEqual(row['developer'], '社团')
        self.assertTrue(row['cover_url'].endswith('RJ123456_img_main.jpg'))
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(clock[0], 4)

    def test_dlsite_wrong_id_and_wrong_cover(self):
        c, _, _ = self.client([(404, b'', 'text/plain'), (200, self.detail('RJ999999'), 'text/html')])
        with self.assertRaises(p.ProviderError) as cm:
            c.details('dlsite', 'RJ123456')
        self.assertEqual(cm.exception.code, 'identity_mismatch')
        c, _, _ = self.client([(404, b'', 'text/plain'), (200, self.detail(image='https://img.dlsite.jp/RJ999999.jpg'), 'text/html')])
        self.assertEqual(c.details('dlsite', 'RJ123456')['cover_url'], '')

    def test_robots_denied_no_detail_request(self):
        c, calls, _ = self.client([(200, b'User-agent: *\nDisallow: /', 'text/plain')])
        for _ in range(2):
            with self.assertRaises(p.ProviderError) as cm:
                c.details('dlsite', 'RJ123456')
            self.assertEqual(cm.exception.code, 'robots_denied')
        self.assertEqual(len(calls), 1)

    def test_robots_server_failure_stops(self):
        c, calls, _ = self.client([(503, b'', 'text/plain')])
        with self.assertRaises(p.ProviderError) as cm:
            c.details('dlsite', 'RJ123456')
        self.assertEqual(cm.exception.code, 'server_error')
        self.assertEqual(len(calls), 1)

    def test_robots_html_is_not_permission(self):
        c, calls, _ = self.client([(200, b'<html>challenge</html>', 'text/html')])
        with self.assertRaises(p.ProviderError) as cm:
            c.details('dlsite', 'RJ123456')
        self.assertEqual(cm.exception.code, 'robots_unavailable')
        self.assertEqual(len(calls), 1)

    def test_vj_and_url_parameter_privacy(self):
        body = self.detail('VJ123456', 'https://img.dlsite.jp/VJ123456.jpg')
        c, calls, _ = self.client([(404, b'', 'text/plain'), (200, body, 'text/html')])
        c.details('dlsite', 'VJ123456')
        self.assertIn('/pro/work/', calls[1][0])
        c, calls, _ = self.client([(404, b'', 'text/plain'), (200, self.detail(), 'text/html')])
        c.search('dlsite', 'https://www.dlsite.com/maniax/work/=/product_id/RJ123456.html?token=private')
        self.assertNotIn('private', calls[1][0])

    def test_cancel_while_waiting_and_request_interval(self):
        c, calls, clock = self.client([(200, b'{"results":[]}', 'application/json')])
        c.next_at['vndb'] = 10
        with self.assertRaises(p.ProviderError) as cm:
            c.search('vndb', 'game', lambda: clock[0] >= .2)
        self.assertEqual(cm.exception.code, 'cancelled')
        self.assertFalse(calls)

    def test_dlsite_name_candidates_and_unknown_page(self):
        body = b'<div class="work_list"><a href="/maniax/work/=/product_id/RJ123456.html">game</a></div>'
        c, _, _ = self.client([(404, b'', 'text/plain'), (200, body, 'text/html')])
        rows = c.search('dlsite', 'name')
        self.assertEqual(rows[0]['external_id'], 'RJ123456')
        self.assertEqual(rows[0]['cover_url'], '')
        c, _, _ = self.client([(404, b'', 'text/plain'), (200, b'<html>challenge</html>', 'text/html')])
        with self.assertRaises(p.ProviderError) as cm:
            c.search('dlsite', 'name')
        self.assertEqual(cm.exception.code, 'parse_error')

    def test_negative_cache_and_cancel(self):
        c, calls, clock = self.client([(200, b'{"results":[]}', 'application/json'), (200, b'{"results":[]}', 'application/json')])
        self.assertEqual(c.search('vndb', 'unknown'), [])
        self.assertEqual(c.search('vndb', 'unknown'), [])
        self.assertEqual(len(calls), 1)
        clock[0] += 61
        c.search('vndb', 'unknown')
        self.assertEqual(len(calls), 2)
        with self.assertRaises(p.ProviderError) as cm:
            c.search('vndb', 'unknown', lambda: True)
        self.assertEqual(cm.exception.code, 'cancelled')

    def test_query_does_not_send_path(self):
        c, calls, _ = self.client([])
        for q in ['D:\\private\\game', '/home/private/game', 'file:///secret', '\\\\server\\folder']:
            with self.assertRaises(p.ProviderError):
                c.search('vndb', q)
        self.assertFalse(calls)

    def test_official_host_only(self):
        c, calls, _ = self.client([])
        for url in ['https://www.dlsite.com.evil.test/x', 'http://www.dlsite.com/x',
                    'https://user@www.dlsite.com/x', 'https://127.0.0.1/x']:
            with self.assertRaises(p.ProviderError):
                c.search('dlsite', url)
        with self.assertRaises(p.ProviderError):
            c.download_cover('https://evil.test/cover.jpg', 'vndb')
        self.assertFalse(calls)

    def test_rate_limit_no_retry(self):
        c, calls, _ = self.client([(429, b'', 'text/plain')])
        with self.assertRaises(p.ProviderError) as cm:
            c.search('vndb', 'game')
        self.assertEqual(cm.exception.code, 'rate_limited')
        self.assertGreaterEqual(cm.exception.retry_after, 60)
        self.assertEqual(len(calls), 1)

    def test_oversize_and_malformed(self):
        for body in [b'x' * (p.MAX_RESPONSE + 1), b'not json']:
            c, _, _ = self.client([(200, body, 'application/json')])
            with self.assertRaises(p.ProviderError):
                c.search('vndb', 'game')

    def test_cover_actual_decode_and_cache(self):
        image = io.BytesIO()
        Image.new('RGB', (20, 20)).save(image, 'PNG')
        c, calls, _ = self.client([(200, image.getvalue(), 'image/png')])
        for _ in range(2):
            self.assertEqual(c.download_cover('https://t.vndb.org/cv/1.png', 'vndb'), image.getvalue())
        self.assertEqual(len(calls), 1)
        c, _, _ = self.client([(200, b'broken', 'image/jpeg')])
        with self.assertRaises(p.ProviderError) as cm:
            c.download_cover('https://t.vndb.org/cv/2.jpg', 'vndb')
        self.assertEqual(cm.exception.code, 'invalid_image')

    def test_redirect_does_not_bypass_checks(self):
        redirect = p._Redirect('dlsite', False)
        with self.assertRaises(p.ProviderError):
            redirect.redirect_request(None, None, 302, '', {}, 'https://evil.test/x')
        with self.assertRaises(p.ProviderError) as cm:
            redirect.redirect_request(None, None, 302, '', {}, 'https://www.dlsite.com/x')
        self.assertEqual(cm.exception.code, 'redirect')


if __name__ == '__main__':
    unittest.main()
