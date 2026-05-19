"""Tests for issue #3: .php → .html conversion and extension-less page handling."""
import sys
import os
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import url_to_local_path


class TestPhpToHtmlConversion(unittest.TestCase):
    """url_to_local_path must save forum .php pages with .html extension."""

    def test_viewforum_php_becomes_html(self):
        path = url_to_local_path("https://visio.getbb.ru/viewforum.php?f=7", Path("out"))
        self.assertTrue(str(path).endswith(".html"), f"Expected .html, got {path}")

    def test_viewtopic_php_becomes_html(self):
        path = url_to_local_path("https://visio.getbb.ru/viewtopic.php?f=7&t=40", Path("out"))
        self.assertTrue(str(path).endswith(".html"), f"Expected .html, got {path}")

    def test_index_php_becomes_html(self):
        path = url_to_local_path("https://visio.getbb.ru/index.php", Path("out"))
        self.assertTrue(str(path).endswith(".html"), f"Expected .html, got {path}")

    def test_viewforum_with_pagination_becomes_html(self):
        path = url_to_local_path(
            "https://visio.getbb.ru/viewforum.php?f=3&start=40", Path("out")
        )
        self.assertTrue(str(path).endswith(".html"), f"Expected .html, got {path}")

    def test_root_url_is_html(self):
        path = url_to_local_path("https://visio.getbb.ru/", Path("out"))
        self.assertTrue(str(path).endswith(".html"), f"Expected .html, got {path}")

    def test_path_without_extension_becomes_html(self):
        # Pages like viewtopic?f=7&t=40 with no .php or other extension
        path = url_to_local_path("https://visio.getbb.ru/viewtopic?f=7&t=40", Path("out"))
        self.assertTrue(str(path).endswith(".html"), f"Expected .html, got {path}")

    def test_no_php_extension_in_filename(self):
        path = url_to_local_path("https://visio.getbb.ru/viewforum.php?f=7", Path("out"))
        self.assertNotIn(".php", str(path), f"Must not contain .php: {path}")

    def test_viewtopic_query_no_php(self):
        path = url_to_local_path("https://visio.getbb.ru/viewtopic.php?f=7&t=40", Path("out"))
        self.assertNotIn(".php", str(path), f"Must not contain .php: {path}")

    def test_query_params_encoded_in_filename(self):
        path = url_to_local_path("https://visio.getbb.ru/viewforum.php?f=3", Path("out"))
        # The query should be embedded in the filename
        self.assertIn("f=3", str(path))

    def test_php_extension_replaced_not_appended(self):
        # Make sure we get viewforum__f=7.html, not viewforum.php__f=7.html
        path = url_to_local_path("https://visio.getbb.ru/viewforum.php?f=7", Path("out"))
        self.assertNotIn("php", str(path).lower(), f"No 'php' should appear in path: {path}")


if __name__ == "__main__":
    unittest.main()
