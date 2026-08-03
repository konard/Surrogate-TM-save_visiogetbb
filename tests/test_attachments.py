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

from parser import (
    ATTACHMENT_ATTEMPTS,
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    THROTTLE_BACKOFF_BASE,
    THROTTLE_BACKOFF_MAX,
    THROTTLE_TRIGGER,
    ForumParser,
    normalize_url,
)


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
        # 1s/2s between attempts, then the throttle cool-down once three
        # consecutive failures show the server is stalling us.
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1.0, 2.0, THROTTLE_BACKOFF_BASE],
        )

    def test_fetch_does_not_retry_forum_pages_by_default(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        parser.session.get = MagicMock(side_effect=requests.ReadTimeout("stall"))

        with patch("parser.time.sleep"):
            self.assertIsNone(parser.fetch("https://visio.getbb.ru/viewtopic.php?t=1"))
        self.assertEqual(parser.session.get.call_count, 1)


def _timeout_response(status: int):
    resp = MagicMock()
    resp.status_code = status
    error = requests.HTTPError(f"{status} error")
    error.response = resp
    resp.raise_for_status.side_effect = error
    return resp


class TestPermanentFailures(unittest.TestCase):
    """A 404 will never succeed, so retrying it only multiplies the cost."""

    def test_permanent_http_error_is_not_retried(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        parser.session.get = MagicMock(return_value=_timeout_response(404))

        with patch("parser.time.sleep"):
            result = parser.download_file("https://visio.getbb.ru/Surrogate")

        self.assertIsNone(result)
        self.assertEqual(parser.session.get.call_count, 1)

    def test_server_error_is_treated_as_transient(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        parser.session.get = MagicMock(return_value=_timeout_response(503))

        with patch("parser.time.sleep"):
            parser.download_file("https://visio.getbb.ru/download/file.php?id=1")

        self.assertEqual(parser.session.get.call_count, ATTACHMENT_ATTEMPTS)

    def test_permanent_failure_is_not_queued_for_retry(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        parser.session.get = MagicMock(return_value=_timeout_response(404))

        with patch("parser.time.sleep"):
            parser.download_file("https://visio.getbb.ru/Surrogate")

        self.assertEqual(parser.retry_queue, {})
        self.assertIn(
            normalize_url("https://visio.getbb.ru/Surrogate"),
            parser.failed_downloads,
        )


class TestFailureMemoization(unittest.TestCase):
    """Issue #17: one stalled attachment was re-fetched 42 times in a run."""

    def test_failed_url_is_not_refetched_within_a_run(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        parser.session.get = MagicMock(side_effect=requests.ReadTimeout("stall"))
        url = "https://visio.getbb.ru/download/file.php?id=1849"

        with patch("parser.time.sleep"):
            for _ in range(10):
                self.assertIsNone(parser.download_file(url))

        # Only the first call reaches the network; the other nine are memoized.
        self.assertEqual(parser.session.get.call_count, ATTACHMENT_ATTEMPTS)

    def test_failed_image_is_not_refetched_within_a_run(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        parser.session.get = MagicMock(side_effect=requests.ReadTimeout("stall"))
        url = "https://visio.getbb.ru/images/ranks/visio_getbb_ru/logo.png"

        with patch("parser.time.sleep"):
            for _ in range(5):
                self.assertIsNone(parser.download_image(url))

        self.assertEqual(parser.session.get.call_count, ATTACHMENT_ATTEMPTS)


class TestDeferredRetryPass(unittest.TestCase):
    """The throttle usually expires by the end of the crawl."""

    def test_deferred_pass_recovers_a_stalled_attachment(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0, retry_passes=1)
        response = MagicMock()
        response.headers = {
            "Content-Disposition": 'attachment; filename="manual.pdf"',
            "Content-Type": "application/pdf",
        }
        response.content = b"pdf"
        response.raise_for_status.return_value = None
        url = "https://visio.getbb.ru/download/file.php?id=2032"

        # Stall for every attempt of the crawl, then succeed once retried later.
        parser.session.get = MagicMock(
            side_effect=[requests.ReadTimeout("stall")] * ATTACHMENT_ATTEMPTS
            + [response]
        )

        with patch("parser.time.sleep"):
            self.assertIsNone(parser.download_file(url))
            recovered = parser.retry_failed_downloads()

        self.assertEqual(recovered, 1)
        self.assertEqual(parser.retry_queue, {})
        self.assertNotIn(normalize_url(url), parser.failed_downloads)
        self.assertIn(normalize_url(url), parser.downloaded_files)

    def test_deferred_pass_is_bounded_and_keeps_hard_failures(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0, retry_passes=2)
        parser.session.get = MagicMock(side_effect=requests.ReadTimeout("stall"))
        url = "https://visio.getbb.ru/download/file.php?id=1849"

        with patch("parser.time.sleep"):
            parser.download_file(url)
            parser.session.get.reset_mock()
            recovered = parser.retry_failed_downloads()

        self.assertEqual(recovered, 0)
        # Two bounded passes, each of ATTACHMENT_ATTEMPTS -- not an open loop.
        self.assertEqual(parser.session.get.call_count, 2 * ATTACHMENT_ATTEMPTS)
        self.assertIn(normalize_url(url), parser.failed_downloads)

    def test_retry_passes_can_be_disabled(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0, retry_passes=0)
        parser.session.get = MagicMock(side_effect=requests.ReadTimeout("stall"))

        with patch("parser.time.sleep"):
            parser.download_file("https://visio.getbb.ru/download/file.php?id=1")
            parser.session.get.reset_mock()
            parser.retry_failed_downloads()

        self.assertEqual(parser.session.get.call_count, 0)


class TestThrottleBackoff(unittest.TestCase):
    def test_backoff_grows_and_is_capped(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        pauses = []
        with patch("parser.time.sleep", side_effect=pauses.append):
            for _ in range(12):
                parser._consecutive_failures += 1
                parser._cool_down_if_throttled()

        self.assertEqual(pauses[0], THROTTLE_BACKOFF_BASE)
        self.assertTrue(all(b <= THROTTLE_BACKOFF_MAX for b in pauses))
        self.assertEqual(pauses[-1], THROTTLE_BACKOFF_MAX)

    def test_no_backoff_below_trigger(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        with patch("parser.time.sleep") as sleep:
            parser._consecutive_failures = THROTTLE_TRIGGER - 1
            parser._cool_down_if_throttled()
        sleep.assert_not_called()

    def test_success_resets_failure_streak(self):
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        response = MagicMock()
        response.raise_for_status.return_value = None
        parser.session.get = MagicMock(
            side_effect=[requests.ReadTimeout("stall"), response]
        )

        with patch("parser.time.sleep"):
            parser.fetch("https://visio.getbb.ru/", attempts=2)

        self.assertEqual(parser._consecutive_failures, 0)


class TestRequestTimeout(unittest.TestCase):
    def test_stalled_request_uses_bounded_timeouts(self):
        """A 60s read timeout burned a full minute per stalled request."""
        parser = ForumParser("/tmp/test_parser_output", delay=0)
        response = MagicMock()
        response.raise_for_status.return_value = None
        parser.session.get = MagicMock(return_value=response)

        parser.fetch("https://visio.getbb.ru/")

        _, kwargs = parser.session.get.call_args
        self.assertEqual(kwargs["timeout"], (CONNECT_TIMEOUT, READ_TIMEOUT))


if __name__ == "__main__":
    unittest.main()
