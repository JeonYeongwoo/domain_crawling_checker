import contextlib
import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import check


class CheckerTests(unittest.TestCase):
    def test_specific_and_merged_groups(self):
        rules = 'User-agent: *\nDisallow: /\nUser-agent: CrawlCheck\nDisallow: /private\nUser-agent: CrawlCheck\nDisallow: /other'
        self.assertTrue(check.robots_allowed(rules, 'https://example.com/news')[0])
        self.assertFalse(check.robots_allowed(rules, 'https://example.com/other')[0])

    def test_longest_rule_and_allow_tie(self):
        rules = 'User-agent: *\nDisallow: /\nAllow: /news\nDisallow: /news/private\nAllow: /news/private'
        self.assertTrue(check.robots_allowed(rules, 'https://example.com/news/private')[0])
        self.assertFalse(check.robots_allowed(rules, 'https://example.com/other')[0])

    def test_wildcards_end_anchor_and_encoding(self):
        rules = 'User-agent: *\nDisallow: /*.pdf$\nDisallow: /~private\nDisallow: /기사'
        self.assertFalse(check.robots_allowed(rules, 'https://example.com/a.pdf')[0])
        self.assertTrue(check.robots_allowed(rules, 'https://example.com/a.pdf?x=1')[0])
        self.assertFalse(check.robots_allowed(rules, 'https://example.com/%7Eprivate')[0])
        self.assertFalse(check.robots_allowed(rules, 'https://example.com/%EA%B8%B0%EC%82%AC')[0])

    def test_invalid_input(self):
        for value in ['ftp://example.com', 'https://', 'https://user:pass@example.com', 'https://example.com:bad']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                check.normalize(value)

    def test_http_scenarios(self):
        scenarios = [
            ('allowed', True), ('blocked', False), ('no_robots', True),
            ('robots_error', False), ('forbidden', False), ('js_shell', False),
            ('challenge', False), ('redirect_blocked', False), ('redirect_allowed', True),
        ]
        for scenario, expected in scenarios:
            with self.subTest(scenario=scenario):
                visited = []
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_GET(self):
                        visited.append(self.path)
                        status, headers = 200, {'Content-Type': 'text/html; charset=utf-8'}
                        body = '<html><body>' + 'News article contents. ' * 20 + '</body></html>'
                        if self.path == '/robots.txt':
                            headers['Content-Type'] = 'text/plain'
                            body = 'User-agent: *\nDisallow: /private' if scenario == 'redirect_blocked' else 'User-agent: *\n'
                            if scenario == 'blocked':
                                body += 'Disallow: /'
                            if scenario == 'no_robots':
                                status = 404
                            if scenario == 'robots_error':
                                status = 503
                        elif self.path == '/':
                            if scenario == 'forbidden':
                                status = 403
                            elif scenario == 'js_shell':
                                body = '<html><script>' + 'secret ' * 100 + '</script></html>'
                            elif scenario == 'challenge':
                                body = '<html>Verify you are human ' + 'checking ' * 30 + '</html>'
                            elif scenario.startswith('redirect_'):
                                status = 302
                                headers['Location'] = '/private' if scenario == 'redirect_blocked' else '/news'
                        self.send_response(status)
                        for key, value in headers.items():
                            self.send_header(key, value)
                        self.end_headers()
                        self.wfile.write(body.encode())
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(check.check('http://127.0.0.1:' + str(server.server_port)), expected)
                    if scenario in ('blocked', 'robots_error'):
                        self.assertEqual(visited, ['/robots.txt'])
                    if scenario == 'redirect_blocked':
                        self.assertNotIn('/private', visited)
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join()


if __name__ == '__main__':
    unittest.main()
