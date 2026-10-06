"""Regression coverage for interrupted and temporarily unavailable archives (#17)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from bs4 import BeautifulSoup

from parser import BASE_URL, ForumParser, url_to_local_path


def response(html):
    result = requests.Response()
    result.status_code = 200
    result.headers['Content-Type'] = 'text/html; charset=utf-8'
    result._content = html.encode()
    result.encoding = 'utf-8'
    return result


class TestCrawlRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.parser = ForumParser(self.tmp.name, delay=0)
        self.addCleanup(self.close_log)
        self.start = BASE_URL + '/viewtopic.php?f=2&t=1129&start=20'
        self.target = BASE_URL + '/viewtopic.php?f=2&t=1129'
        self.html = ('<a name="p13012"></a><a href="viewtopic.php?p=12277#p12277">quote</a>'
                     '<a href="viewtopic.php?f=2&amp;t=1129">1</a>')

    def close_log(self):
        if self.parser._download_log_handler:
            self.parser._download_log_handler.close()

    def write(self, url, html):
        path = url_to_local_path(url, self.output)
        path.write_text(html, encoding='utf-8')
        return path

    def test_transient_page_failure_is_recovered_and_quote_becomes_local(self):
        self.parser.session.get = MagicMock(side_effect=[
            response(self.html), requests.ReadTimeout('stall'),
            response('<a name="p12277"></a>quoted post'),
        ])
        with patch('parser.time.sleep'):
            self.parser.crawl(self.start)
        html = url_to_local_path(self.start, self.output).read_text()
        self.assertIn('viewtopic__f=2&amp;t=1129.html#p12277', html)
        self.assertEqual(self.parser.session.get.call_count, 3)

    def test_failed_page_attempts_are_bounded_and_reported(self):
        self.parser.session.get = MagicMock(side_effect=requests.ReadTimeout('outage'))
        with patch('parser.time.sleep'):
            self.parser.crawl(self.start)
        self.assertEqual(self.parser.session.get.call_count, 3)
        pending = json.loads((self.output / 'failed_pages.json').read_text())
        self.assertIn(self.start, pending)

    def test_permanent_page_failure_is_not_retried(self):
        error = response('missing')
        error.status_code = 404
        self.parser.session.get = MagicMock(return_value=error)
        with patch('parser.time.sleep'):
            self.parser.crawl(self.start)
        self.assertEqual(self.parser.session.get.call_count, 1)

    def test_resume_traverses_saved_pages_without_fetching_them(self):
        self.parser.resume = True
        self.write(self.start, self.parser.process_page(self.start, self.html))
        self.parser.queue.clear()
        self.parser.session.get = MagicMock(return_value=response('<a name="p12277"></a>post'))
        self.parser.crawl(self.start)
        self.assertTrue(url_to_local_path(self.target, self.output).exists())
        self.parser.session.get.assert_called_once_with(
            self.target, timeout=(15.0, 20.0), allow_redirects=True)

    def test_interrupt_finalizes_quotes_before_propagating(self):
        self.write(self.target, '<a name="p12277"></a>post')
        self.parser.session.get = MagicMock(side_effect=[response(self.html), KeyboardInterrupt])
        with self.assertRaises(KeyboardInterrupt):
            self.parser.crawl(self.start)
        self.assertTrue((self.output / 'posts.json').exists())
        self.assertIn('viewtopic__f=2&amp;t=1129.html#p12277',
                      url_to_local_path(self.start, self.output).read_text())

    def test_repair_existing_online_quote_without_marker(self):
        path = self.write(self.start, '<a href="https://visio.getbb.ru/viewtopic.php?p=12277#p12277">quote</a>')
        self.write(self.target, '<a name="p12277"></a>post')
        self.assertEqual(self.parser.resolve_post_links(), 1)
        self.assertEqual(BeautifulSoup(path.read_text(), 'html.parser').a['href'],
                         'viewtopic__f=2&t=1129.html#p12277')
