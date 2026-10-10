"""Repair archives made by older parser copies (PR #18, 8 October feedback)."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from parser import BASE_URL, ForumParser, _remove_forum_chrome, main


LEGACY_TOPIC = (Path(__file__).parent / 'fixtures' / 'legacy-topic.html').read_text(encoding='utf-8')
PAGE = 'viewtopic__f=3&t=1571.html'
TARGET = 'viewtopic__f=3&t=1571&start=20.html'


class TestLegacyCleanup(unittest.TestCase):
    def test_local_topic_controls_and_permalink_labels(self):
        soup = BeautifulSoup(LEGACY_TOPIC, 'html.parser')
        _remove_forum_chrome(soup)
        text = soup.get_text(' ', strip=True)
        for label in ('На страницу', 'След.', 'Для печати', 'Пред. тема', 'След. тема', 'можете'):
            self.assertNotIn(label, text)
        self.assertIsNotNone(soup.find('a', string='2'))
        self.assertEqual(soup.img['alt'], '#p15875')
        self.assertEqual(soup.img['title'], '#p15875')
        self.assertEqual(soup.img.parent.find_next_sibling('b').get_text(), '\u00a0\u00a0Добавлено:')

    def test_permissions_for_logged_in_users_removed(self):
        soup = BeautifulSoup('<div id="pagecontent"><div class="postbody">Post</div></div>'
                             '<table><tr><td>Вы <strong>можете</strong> начинать темы<br>'
                             'Вы <strong>можете</strong> добавлять вложения</td></tr></table>', 'html.parser')
        _remove_forum_chrome(soup)
        self.assertNotIn('можете', soup.get_text())
        self.assertIn('Post', soup.get_text())

    def test_footer_signatures_inside_post_do_not_delete_content(self):
        soup = BeautifulSoup('<div id="pagecontent"><table><tr><td><div class="postbody">'
                             '<table><tr><td>Вы не можете добавлять вложения</td></tr></table>'
                             'Useful explanation</div></td></tr></table></div>', 'html.parser')
        _remove_forum_chrome(soup)
        self.assertIn('Useful explanation', soup.get_text())
        self.assertIn('Вы не можете добавлять вложения', soup.get_text())


class TestArchiveRepair(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name)
        self.page = self.output / PAGE
        self.page.write_text(LEGACY_TOPIC, encoding='utf-8')
        (self.output / TARGET).write_text('<a name="p19537"></a><div class="postbody">Target</div>', encoding='utf-8')
        self.parser = ForumParser(tmp.name, resume=True, delay=0)
        self.addCleanup(self.parser.session.close)

    def test_resolve_legacy_local_post_links_and_missing_target(self):
        with patch('requests.Session.get', side_effect=AssertionError('Offline repair must not fetch')):
            self.parser.resolve_post_links()
        soup = BeautifulSoup(self.page.read_text(encoding='utf-8'), 'html.parser')
        self.assertEqual(soup.img.parent['href'], '#p15875')
        self.assertEqual(soup.find('a', string='Цитируемое сообщение #19537')['href'], TARGET + '#p19537')
        self.assertEqual(soup.find('a', string='Сообщение вне архива')['href'],
                         BASE_URL + '/viewtopic.php?p=99999#p99999')
        self.assertEqual(soup.find('a', string='Внешний форум')['href'], 'https://example.org/viewtopic.php?p=15875')
        self.assertEqual(soup.find('a', string='Скачанное вложение')['href'], 'download/file_1849.vsd')

    def test_offline_repair_is_idempotent_and_rebuilds_index(self):
        with patch('requests.Session.get', side_effect=AssertionError('Offline repair must not fetch')):
            self.parser.repair_archive()
            first = self.page.read_text(encoding='utf-8')
            self.parser.repair_archive()
        self.assertEqual(self.page.read_text(encoding='utf-8'), first)
        soup = BeautifulSoup(first, 'html.parser')
        self.assertEqual(soup.img['alt'], '#p15875')
        self.assertNotIn('На страницу', soup.get_text())
        self.assertNotIn('можете', soup.get_text())
        self.assertEqual(json.loads((self.output / 'posts.json').read_text())['19537'],
                         BASE_URL + '/viewtopic.php?f=3&t=1571&start=20')
        self.assertTrue((self.output / 'forum.json').exists())

    def test_resume_repairs_cached_page_before_discovery(self):
        with patch.object(self.parser, 'fetch', side_effect=AssertionError('Cached page must not fetch')):
            self.parser.save_page(BASE_URL + '/viewtopic.php?f=3&t=1571')
        soup = BeautifulSoup(self.page.read_text(encoding='utf-8'), 'html.parser')
        self.assertEqual(soup.img['alt'], '#p15875')
        self.assertNotIn('можете', soup.get_text())
        self.assertEqual(list(self.parser.queue), [BASE_URL + '/viewtopic.php?f=3&t=1571&start=20'])

    def test_repair_cli_does_not_crawl(self):
        with patch('sys.argv', ['parser.py', '-o', str(self.output), '--repair-archive']), \
                patch('requests.Session.get', side_effect=AssertionError('Offline repair must not fetch')):
            main()
        self.assertEqual(BeautifulSoup(self.page.read_text(encoding='utf-8'), 'html.parser').img['alt'], '#p15875')

    def test_duplicate_post_page_keeps_own_permalink(self):
        # Older archives contain both topic and p-only copies of the same page.
        duplicate = self.output / 'viewtopic__f=1&t=1.html'
        duplicate.write_text('<a name="p15875"></a>', encoding='utf-8')
        self.parser.resolve_post_links()
        self.assertEqual(BeautifulSoup(self.page.read_text(encoding='utf-8'), 'html.parser').img.parent['href'], '#p15875')

    def test_wrong_same_page_quote_from_old_parser_repaired(self):
        self.page.write_text('<a href="#p19537">quote</a><a href="#p99999">missing</a>', encoding='utf-8')
        self.parser.resolve_post_links()
        links = BeautifulSoup(self.page.read_text(encoding='utf-8'), 'html.parser').find_all('a')
        self.assertEqual(links[0]['href'], TARGET + '#p19537')
        self.assertEqual(links[1]['href'], BASE_URL + '/viewtopic.php?p=99999#p99999')
        self.assertEqual(self.parser.resolve_post_links(), 0)

    def test_startup_log_identifies_script_and_effective_timeouts(self):
        with patch.object(self.parser, '_drain_page_queue'), \
                patch.object(self.parser, 'retry_failed_downloads'), \
                self.assertLogs('parser', level='INFO') as logs:
            self.parser.crawl()
        self.addCleanup(self.parser._download_log_handler.close)
        startup = logs.output[0]
        self.assertIn('ForumArchiver 2026.10.10', startup)
        self.assertIn('script=', startup)
        self.assertIn('read_timeout=20s', startup)
        self.assertIn('retry_passes=2', startup)
        self.assertIn('attachment_attempts=3', startup)
        download_log = (self.output / 'downloads.log').read_text(encoding='utf-8')
        self.assertIn('ForumArchiver 2026.10.10', download_log)

    def test_version_cli_identifies_build_without_crawling(self):
        output = io.StringIO()
        with patch('sys.argv', ['parser.py', '--version']), contextlib.redirect_stdout(output), \
                patch('requests.Session.get', side_effect=AssertionError('Version must not fetch')), \
                self.assertRaises(SystemExit) as result:
            main()
        self.assertEqual(result.exception.code, 0)
        self.assertIn('2026.10.10', output.getvalue())


if __name__ == '__main__':
    unittest.main()
