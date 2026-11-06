import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(app, "STORE_PATH", self.root / "sites.json"),
                        patch.object(app, "REPORT_PATH", self.root / "results.txt"),
                        patch.object(app, "DOMAINS_PATH", self.root / "domains.txt"),
                        patch.object(app, "URLS_PATH", self.root / "urls.txt")]
        for item in self.patches:
            item.start()
        self.addCleanup(self.temp.cleanup)
        for item in self.patches:
            self.addCleanup(item.stop)

    def test_first_run_preserves_user_results_and_restart(self):
        sites = app.load_sites()
        self.assertEqual(len(sites), 6)
        self.assertEqual([s['status'] for s in sites], [True] * 5 + [False])
        self.assertTrue(all(s['result_source'] == '사용자 제공' for s in sites))
        self.assertEqual(app.load_sites(), sites)
        app.export_report(sites)
        text = app.REPORT_PATH.read_text(encoding='utf-8')
        self.assertIn('크롤링 가능 (5개)', text)
        self.assertIn('크롤링 불가능 (1개)', text)
        self.assertIn('사용자 제공·재검사 전', text)

    def test_discovery_reads_sources_and_deduplicates(self):
        rss = '''<rss><channel>
          <item><link>https://news.google.com/rss/articles/opaque</link><source url="https://example.com">Example News</source></item>
          <item><source url="https://www.example.com/">Duplicate</source></item>
          <item><source url="https://another.com">Other</source></item>
          <item><source url="ftp://bad.com">Bad</source></item>
          <item><source>No address</source></item>
        </channel></rss>'''
        with patch.object(app.check, 'fetch', return_value=(200, {}, rss, False)) as fetch:
            with contextlib.redirect_stdout(io.StringIO()):
                found = app.discover('인공지능 뉴스')
        self.assertEqual(found, [('Example News', 'https://example.com/'), ('Other', 'https://another.com/')])
        self.assertIn('q=%EC%9D%B8', fetch.call_args.args[0])

    def test_discovery_failure_does_not_overwrite_database(self):
        app.load_sites()
        before = app.STORE_PATH.read_bytes()
        with patch.object(app.check, 'fetch', return_value=(503, {}, '', False)):
            with self.assertRaises(ValueError), contextlib.redirect_stdout(io.StringIO()):
                app.discover('뉴스')
        self.assertEqual(app.STORE_PATH.read_bytes(), before)

    def test_duplicate_domain_keeps_user_news_path(self):
        sites = app.load_sites()
        self.assertFalse(app.add_site(sites, 'KBS', 'https://news.kbs.co.kr/', '탐색'))
        self.assertEqual(sites[4]['url'], app.SEEDS[4][1])
        self.assertTrue(app.add_site(sites, '새 뉴스', 'new.example.com', '직접 추가'))
        self.assertIsNone(sites[-1]['status'])

    def test_checks_save_latest_status_logs_and_report(self):
        sites = app.load_sites()
        def failure(url):
            app.check.log('robots.txt 판단: disallow: /')
            return False
        with patch.object(app.check, 'check', side_effect=failure), contextlib.redirect_stdout(io.StringIO()):
            app.inspect_site(sites[0])
        app.save_sites(sites)
        app.export_report(sites)
        restored = app.load_sites()[0]
        self.assertFalse(restored['status'])
        self.assertEqual(restored['result_source'], '앱 검사')
        self.assertIn('disallow: /', restored['reason'])
        self.assertTrue(restored['checked_at'])
        self.assertEqual(len(restored['logs']), 1)
        self.assertIn('크롤링 불가능 (2개)', app.REPORT_PATH.read_text(encoding='utf-8'))

    def test_invalid_store_not_overwritten(self):
        app.STORE_PATH.write_text('{bad data', encoding='utf-8')
        with self.assertRaises(ValueError):
            app.load_sites()
        self.assertEqual(app.STORE_PATH.read_text(encoding='utf-8'), '{bad data')

    def test_menu_discover_add_inspect_restart(self):
        inputs = ['1', '1', 'AI', '3', 'https://manual.example.com', '수동 뉴스',
                  '2', '7,8', '4', '5', '7', 'q']
        with patch('builtins.input', side_effect=inputs), \
             patch.object(app, 'discover', return_value=[('탐색 뉴스', 'https://found.example.com')]), \
             patch.object(app.check, 'check', return_value=True), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.run(), 0)
        sites = app.load_sites()
        self.assertEqual(len(sites), 8)
        self.assertTrue(all(s['status'] is True for s in sites[-2:]))
        self.assertIn('신규 저장 1개', output.getvalue())
        self.assertIn('수동 뉴스', app.REPORT_PATH.read_text(encoding='utf-8'))

    def test_manual_url_change_clears_previous_result(self):
        sites = app.load_sites()
        with patch('builtins.input', side_effect=['https://news.kbs.co.kr/new-path', 'y']), \
             contextlib.redirect_stdout(io.StringIO()):
            app.manual_menu(sites)
        self.assertIsNone(app.load_sites()[4]['status'])
        self.assertEqual(app.load_sites()[4]['url'], 'https://news.kbs.co.kr/new-path')

    def test_export_filters_deduplicates_and_preserves_checked_paths(self):
        sites = [
            {'url': 'https://news.example.com/news/main', 'status': True, 'result_source': '앱 검사'},
            {'url': 'https://news.example.com/news/other', 'status': True, 'result_source': '사용자 제공'},
            {'url': 'https://news.example.com/news/main', 'status': True},
            {'url': 'https://blocked.example.com/', 'status': False},
            {'url': 'https://pending.example.com/', 'status': None},
        ]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            app.export_crawlable(sites)
        self.assertEqual(app.DOMAINS_PATH.read_text(encoding='utf-8'), 'news.example.com\n')
        self.assertEqual(app.URLS_PATH.read_text(encoding='utf-8').splitlines(),
                         ['https://news.example.com/news/main', 'https://news.example.com/news/other'])
        self.assertIn('사용자 제공·재검사 전 결과 1개', output.getvalue())

    def test_export_menu_clears_old_file_when_no_sites_are_allowed(self):
        sites = app.load_sites()
        for site in sites:
            site['status'] = False
        app.save_sites(sites)
        app.DOMAINS_PATH.write_text('stale.example.com\n', encoding='utf-8')
        app.URLS_PATH.write_text('https://stale.example.com/\n', encoding='utf-8')
        with patch('builtins.input', side_effect=['6', 'q']), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.run(), 0)
        self.assertEqual(app.DOMAINS_PATH.read_text(encoding='utf-8'), '')
        self.assertEqual(app.URLS_PATH.read_text(encoding='utf-8'), '')
        self.assertIn('빈 목록을 저장했습니다', output.getvalue())

    def test_selection_validation_and_candidates(self):
        sites = app.load_sites()
        self.assertEqual(len(app.select_sites(sites, 'new')), 6)
        self.assertEqual(app.select_sites(sites, '1,1,6'), [sites[0], sites[5]])
        for value in ('0', '-1', '7', 'a', '1,'):
            with self.assertRaises(ValueError):
                app.select_sites(sites, value)
        self.assertEqual(len(app.import_candidates()), 67)


if __name__ == '__main__':
    unittest.main()
