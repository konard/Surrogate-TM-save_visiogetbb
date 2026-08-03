"""Regression tests for attachment discovery and request throttling (issue #15)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import ATTACHMENT_ATTEMPTS, ForumParser


ATTACHMENT_HTML = """\
<html><body><div id="pagecontent">
  <a href="./download/file.php?id=101&amp;sid=deadbeef">manual.pdf</a>
  <img src="./download/file.php?id=102&amp;t=1&amp;sid=deadbeef" alt="diagram.png">
  <iframe src="https://view.officeapps.live.com/op/view.aspx?src=https://visio.getbb.ru/download/file.php?id=103&amp;wdOrigin=BROWSELINK"></iframe>
</div></body></html>
"""


class TestAttachmentDiscovery(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.parser = ForumParser(self.tempdir.name)
        self.parser.download_image = MagicMock(return_value=None)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_downloads_link_image_and_office_iframe_attachments(self):
        downloaded = {
            101: Path(self.tempdir.name) / "download" / "file_101.pdf",
            102: Path(self.tempdir.name) / "download" / "file_102.png",
            103: Path(self.tempdir.name) / "download" / "file_103.xlsx",
        }

        def fake_download(url):
            for file_id, path in downloaded.items():
                if f"id={file_id}" in url:
                    return path
            return None

        self.parser.download_file = MagicMock(side_effect=fake_download)
        result = self.parser.process_page(
            "https://visio.getbb.ru/viewtopic.php?t=1", ATTACHMENT_HTML
        )
        soup = BeautifulSoup(result, "html.parser")

        called_urls = [call.args[0] for call in self.parser.download_file.call_args_list]
        self.assertEqual(len(called_urls), 3)
        self.assertTrue(any("id=101" in url for url in called_urls))
        self.assertTrue(any("id=102" in url for url in called_urls))
        self.assertTrue(any("id=103" in url for url in called_urls))
        self.assertEqual(soup.find("a")["href"], "download/file_101.pdf")
        self.assertEqual(soup.find("img")["src"], "download/file_102.png")
        self.assertEqual(soup.find("iframe")["src"], "download/file_103.xlsx")


class TestRequestDelay(unittest.TestCase):
    def test_delay_is_applied_once_before_each_request(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0.1)
        response = MagicMock()
        response.raise_for_status.return_value = None
        parser.session.get = MagicMock(return_value=response)

        with patch("parser.time.sleep") as sleep:
            self.assertIs(parser.fetch("https://example.com/one"), response)
            self.assertIs(parser.fetch("https://example.com/two"), response)

        self.assertEqual(sleep.call_count, 1)
        actual_delay = sleep.call_args.args[0]
        self.assertGreater(actual_delay, 0)
        self.assertLessEqual(actual_delay, 0.1)


class TestAttachmentRetries(unittest.TestCase):
    def test_download_file_retries_transient_timeout(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        response = MagicMock()
        response.headers = {
            "Content-Disposition": 'attachment; filename="manual.pdf"',
            "Content-Type": "application/pdf",
        }
        response.content = b"pdf"
        response.raise_for_status.return_value = None
        parser.session.get = MagicMock(
            side_effect=[requests.ReadTimeout("temporary stall"), response]
        )

        with patch("parser.time.sleep") as sleep:
            result = parser.download_file(
                "https://visio.getbb.ru/download/file.php?id=2032"
            )

        self.assertIsNotNone(result)
        self.assertEqual(parser.session.get.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_download_file_stops_after_bounded_attempts(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        parser.session.get = MagicMock(side_effect=requests.ReadTimeout("stall"))

        with patch("parser.time.sleep") as sleep:
            result = parser.download_file(
                "https://visio.getbb.ru/download/file.php?id=2032"
            )

        self.assertIsNone(result)
        self.assertEqual(parser.session.get.call_count, ATTACHMENT_ATTEMPTS)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1.0, 2.0],
        )

    def test_fetch_does_not_retry_forum_pages_by_default(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        parser.session.get = MagicMock(side_effect=requests.ReadTimeout("stall"))

        self.assertIsNone(parser.fetch("https://visio.getbb.ru/viewtopic.php?t=1"))
        self.assertEqual(parser.session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
