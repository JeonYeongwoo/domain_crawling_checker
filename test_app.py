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
                        patch.object(app, "URLS_PATH", self.root / "urls.txt"),
                        patch.object(app, "FILE_CHECKS_PATH", self.root / "file_checks")]
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
        inputs = ['1', '1', '1', 'AI', '2', 'https://manual.example.com', '수동 뉴스',
                  'b', '2', '1', '7,8', 'b', '3', '1', '2', '7', 'q']
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
        with patch('builtins.input', side_effect=['3', '3', 'q']), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.run(), 0)
        self.assertEqual(app.DOMAINS_PATH.read_text(encoding='utf-8'), '')
        self.assertEqual(app.URLS_PATH.read_text(encoding='utf-8'), '')
        self.assertIn('빈 목록을 저장했습니다', output.getvalue())

    def test_file_upload_menu_saves_valid_domains_and_keeps_previous_results(self):
        sites = app.load_sites()
        original = dict(sites[1])
        path = self.root / '업로드 목록.txt'
        path.write_text('# comment\n\nnews.jtbc.co.kr\nnew.example.com\n새 뉴스 : https://other.example.com/news\nnot a domain\nftp://bad.example.com\nnew.example.com\n', encoding='utf-8-sig')
        with patch('builtins.input', side_effect=['1', '3', '"' + str(path) + '"', 'q']), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.run(), 0)
        saved = app.load_sites()
        self.assertEqual(len(saved), 8)
        self.assertEqual(saved[1], original)
        self.assertEqual(saved[-1]['name'], '새 뉴스')
        self.assertEqual(saved[-1]['url'], 'https://other.example.com/news')
        self.assertIsNone(saved[-1]['status'])
        self.assertIn('신규 2개 / 중복 2개 / 잘못된 줄 2개', output.getvalue())

    def test_file_picker_and_cp949(self):
        path = self.root / 'list.txt'
        path.write_bytes('한국 뉴스 : https://news.example.com/\n'.encode('cp949'))
        sites = []
        with patch('builtins.input', return_value=''), \
             patch.object(app, 'select_domain_file', return_value=str(path)), \
             contextlib.redirect_stdout(io.StringIO()):
            app.upload_menu(sites)
        self.assertEqual(app.load_sites()[0]['name'], '한국 뉴스')

    def test_invalid_or_cancelled_upload_does_not_change_store(self):
        sites = app.load_sites()
        before = app.STORE_PATH.read_bytes()
        with patch('builtins.input', return_value=''), \
             patch.object(app, 'select_domain_file', return_value=''), \
             contextlib.redirect_stdout(io.StringIO()):
            app.upload_menu(sites)
        invalid = self.root / 'invalid.txt'
        invalid.write_text('invalid line\n', encoding='utf-8')
        with patch('builtins.input', return_value=str(invalid)), contextlib.redirect_stdout(io.StringIO()):
            app.upload_menu(sites)
        self.assertEqual(app.STORE_PATH.read_bytes(), before)
        large = self.root / 'large.txt'
        large.write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            app.read_domain_file(large)
        many = self.root / 'many.txt'
        many.write_text('example.com\n' * 10001, encoding='utf-8')
        with self.assertRaises(ValueError):
            app.read_domain_file(many)

    def test_delete_selected_domains_persists_and_refreshes_exports(self):
        sites = app.load_sites()
        with contextlib.redirect_stdout(io.StringIO()):
            app.export_crawlable(sites)
        with patch('builtins.input', return_value='1,3,1'), contextlib.redirect_stdout(io.StringIO()):
            app.delete_menu(sites)
        self.assertEqual(len(sites), 4)
        self.assertEqual(app.load_sites(), sites)
        self.assertNotIn('ichannela.com', app.DOMAINS_PATH.read_text(encoding='utf-8'))
        self.assertNotIn(app.SEEDS[2][1], app.URLS_PATH.read_text(encoding='utf-8').splitlines())
        self.assertNotIn('채널 A', app.REPORT_PATH.read_text(encoding='utf-8'))

    def test_delete_cancel_invalid_and_write_failure_preserve_sites(self):
        sites = app.load_sites()
        before = app.STORE_PATH.read_bytes()
        with patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            app.delete_menu(sites)
        for value in ['0', '7', 'new', 'bad', '1,']:
            with patch('builtins.input', return_value=value), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                app.delete_menu(sites)
        with patch('builtins.input', return_value='1'), patch.object(app, 'save_sites', side_effect=OSError('disk full')), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(OSError):
            app.delete_menu(sites)
        self.assertEqual(len(sites), 6)
        self.assertEqual(app.STORE_PATH.read_bytes(), before)

    def test_delete_all_stays_empty_after_restart(self):
        sites = app.load_sites()
        with patch('builtins.input', return_value='all'), contextlib.redirect_stdout(io.StringIO()):
            app.delete_menu(sites)
        self.assertEqual(sites, [])
        self.assertEqual(app.load_sites(), [])

    def test_file_check_menu_checks_only_file_and_keeps_distinct_paths(self):
        app.load_sites()
        before = app.STORE_PATH.read_bytes()
        path = self.root / 'test news.txt'
        path.write_text('JTBC : https://news.jtbc.co.kr/\nhttps://news.jtbc.co.kr/private\nhttps://news.jtbc.co.kr/private\nwrong line\n', encoding='utf-8')
        def checker(url):
            result = not url.endswith('/private')
            app.check.log('접근 성공' if result else 'robots.txt 차단')
            return result
        with patch('builtins.input', side_effect=['2', '2', str(path), 'q']), \
             patch.object(app.check, 'check', side_effect=checker) as check_url, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.run(), 0)
        self.assertEqual(check_url.call_count, 2)
        self.assertEqual(app.STORE_PATH.read_bytes(), before)
        directories = list(app.FILE_CHECKS_PATH.iterdir())
        self.assertEqual(len(directories), 1)
        directory = directories[0]
        self.assertEqual((directory / 'possible_urls.txt').read_text(encoding='utf-8'), 'https://news.jtbc.co.kr/\n')
        self.assertEqual((directory / 'impossible_urls.txt').read_text(encoding='utf-8'), 'https://news.jtbc.co.kr/private\n')
        text = (directory / 'results.txt').read_text(encoding='utf-8')
        self.assertIn('크롤링 가능 (1개)', text)
        self.assertIn('크롤링 불가능 (1개)', text)
        self.assertIn('제외된 줄 4', text)
        self.assertIn('robots.txt 차단', (directory / 'results.json').read_text(encoding='utf-8'))
        self.assertIn('정상 URL 2개 / 중복 1개 / 잘못된 줄 1개', output.getvalue())

    def test_interrupted_file_check_saves_completed_and_pending(self):
        path = self.root / 'interrupt.txt'
        path.write_text('first.example.com\nsecond.example.com\n', encoding='utf-8')
        with patch('builtins.input', return_value=str(path)), \
             patch.object(app.check, 'check', side_effect=[True, KeyboardInterrupt]), \
             contextlib.redirect_stdout(io.StringIO()):
            app.inspect_file_menu()
        directory = next(app.FILE_CHECKS_PATH.iterdir())
        text = (directory / 'results.txt').read_text(encoding='utf-8')
        self.assertIn('크롤링 가능 (1개)', text)
        self.assertIn('미검사 (1개)', text)
        self.assertEqual((directory / 'impossible_urls.txt').read_text(encoding='utf-8'), '')

    def test_cancelled_or_invalid_file_check_does_not_start_requests(self):
        for value in ['', str(self.root / 'invalid.txt')]:
            (self.root / 'invalid.txt').write_text('not a domain\n', encoding='utf-8')
            with patch('builtins.input', return_value=value), patch.object(app, 'select_domain_file', return_value=''), \
                 patch.object(app.check, 'check') as checker, contextlib.redirect_stdout(io.StringIO()):
                app.inspect_file_menu()
                checker.assert_not_called()
        self.assertFalse(app.FILE_CHECKS_PATH.exists())

    def test_each_submenu_number_routes_to_correct_function(self):
        routes = [('1', '1', 'explore_menu'), ('1', '2', 'manual_menu'),
                  ('1', '3', 'upload_menu'), ('1', '4', 'delete_menu'),
                  ('2', '1', 'inspect_menu'), ('2', '2', 'inspect_file_menu'),
                  ('3', '1', 'show_saved_results'), ('3', '2', 'logs_menu'),
                  ('3', '3', 'export_crawlable')]
        for category, number, function in routes:
            with self.subTest(category=category, number=number), \
                 patch('builtins.input', side_effect=[category, number, '0', 'q']), \
                 patch.object(app, function) as handler, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.run(), 0)
                if function == 'inspect_file_menu':
                    handler.assert_called_once_with()
                else:
                    handler.assert_called_once_with(app.load_sites())

    def test_submenu_back_and_invalid_choice_do_not_execute_actions(self):
        with patch('builtins.input', side_effect=['1', '9', 'b', '2', '0', '0']), \
             patch.object(app, 'delete_menu') as delete, \
             patch.object(app, 'inspect_menu') as inspect, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.run(), 0)
        delete.assert_not_called()
        inspect.assert_not_called()
        self.assertIn('1~4 중 선택하세요.', output.getvalue())

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
