"""Tests for issue #13: remove forum chrome from saved HTML pages."""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock
from pathlib import Path

from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import parser
from parser import ForumParser, _remove_forum_chrome


assert Path(parser.__file__).resolve() == REPO_ROOT / "parser.py"


FORUM_CHROME_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div id="menubar">
  <table><tr><td><a href="./ucp.php?mode=login&amp;sid=abc">Вход</a></td></tr></table>
</div>
<div id="datebar">
  <table><tr><td class="gensmall"></td><td class="gensmall" align="right">Текущее время: Май 22 2026, 7:40<br></td></tr></table>
</div>
<p class="searchbar">
  <span style="float: left;"><a href="./search.php?search_id=unanswered&amp;sid=abc">Сообщения без ответов</a></span>
</p>
<div id="pagecontent">
  <table width="100%" cellspacing="1">
    <tr>
      <td align="left" valign="middle" nowrap="nowrap">
        <a href="./posting.php?mode=post&amp;f=29&amp;sid=abc"><img alt="Начать новую тему" src="./styles/subsilver2-modded/imageset/ru/button_topic_new.gif"></a>
        <a href="./posting.php?mode=reply&amp;f=29&amp;t=236&amp;sid=abc"><img alt="Ответить на тему" src="./styles/subsilver2-modded/imageset/ru/button_topic_reply.gif"></a>
      </td>
      <td align="right">Страница <strong>1</strong> из <strong>1</strong></td>
    </tr>
  </table>
  <table width="100%" cellspacing="1" class="tablebg">
    <tr>
      <td class="row1">
        <div class="postbody">
          Useful topic content
          <span onclick="toggle" /><b>Спойлер: </b><a href="#" onclick="return false;">↕</a></span>
          <div class="quotecontent"><div style="display: none;">Hidden topic details</div></div>
        </div>
      </td>
    </tr>
  </table>
  <table width="100%" cellspacing="1" class="tablebg">
    <tr align="center"><td class="cat"><form name="viewtopic"><span class="gensmall">Показать сообщения за:</span></form></td></tr>
  </table>
  <table width="100%" cellspacing="1">
    <tr>
      <td align="left" valign="middle" nowrap="nowrap">
        <a href="./posting.php?mode=reply&amp;f=29&amp;t=236&amp;sid=abc"><img alt="Ответить на тему" src="./styles/subsilver2-modded/imageset/ru/button_topic_reply.gif"></a>
      </td>
      <td align="right">Вернуться к началу</td>
    </tr>
  </table>
</div>
<div id="pagefooter">
  <p class="datetime">Часовой пояс: UTC + 3 часа [ Летнее время ]</p>
  <table width="100%" cellspacing="1" class="tablebg"><tr><td>Footer links</td></tr></table>
  <div class="copyright">Powered by phpBB</div>
</div>
</body>
</html>"""


class TestForumChromeCleanup(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.parser = ForumParser(output_dir=self.tempdir.name)
        self.parser.download_image = MagicMock(return_value=None)
        self.parser.download_file = MagicMock(return_value=None)

    def tearDown(self):
        self.tempdir.cleanup()

    def process(self) -> BeautifulSoup:
        result = self.parser.process_page(
            "https://visio.getbb.ru/viewtopic.php?f=29&t=236",
            FORUM_CHROME_HTML,
        )
        return BeautifulSoup(result, "html.parser")

    def test_cleanup_helper_removes_forum_chrome_directly(self):
        soup = BeautifulSoup(FORUM_CHROME_HTML, "html.parser")

        _remove_forum_chrome(soup)

        self.assertIsNone(soup.find(id="menubar"))
        self.assertIsNone(soup.find(id="datebar"))
        self.assertIsNone(soup.find("p", class_="searchbar"))
        self.assertIsNone(soup.find("form", attrs={"name": "viewtopic"}))
        self.assertIsNone(soup.find(id="pagefooter").find("p", class_="datetime"))
        self.assertIsNotNone(soup.find("div", class_="postbody"))

    def test_removes_menubar_datebar_and_searchbar(self):
        soup = self.process()

        self.assertIsNone(soup.find(id="menubar"))
        self.assertIsNone(soup.find(id="datebar"))
        self.assertIsNone(soup.find("p", class_="searchbar"))

    def test_removes_topic_action_cells_but_keeps_adjacent_navigation(self):
        soup = self.process()
        pagecontent = soup.find(id="pagecontent")

        self.assertIsNotNone(pagecontent)
        self.assertNotIn("Начать новую тему", pagecontent.get_text(" "))
        self.assertNotIn("Ответить на тему", pagecontent.get_text(" "))
        self.assertIn("Страница", pagecontent.get_text(" "))
        self.assertIn("Вернуться к началу", pagecontent.get_text(" "))

    def test_removes_viewtopic_sort_tablebg_inside_pagecontent(self):
        soup = self.process()
        pagecontent = soup.find(id="pagecontent")

        self.assertIsNotNone(pagecontent)
        self.assertIsNone(pagecontent.find("form", attrs={"name": "viewtopic"}))
        self.assertIn("Useful topic content", pagecontent.get_text(" "))
        self.assertIn("Hidden topic details", pagecontent.get_text(" "))
        self.assertIsNotNone(pagecontent.find("div", class_="postbody"))

    def test_preserves_real_topic_table_with_spoiler_after_sort_table_removal(self):
        soup = self.process()
        pagecontent = soup.find(id="pagecontent")

        self.assertIsNotNone(pagecontent)
        post_table = pagecontent.find("div", class_="postbody").find_parent("table")

        self.assertIsNotNone(post_table)
        self.assertIn("tablebg", post_table.get("class", []))
        self.assertIn("Useful topic content", post_table.get_text(" "))
        self.assertIn("Hidden topic details", post_table.get_text(" "))

    def test_pagefooter_keeps_only_tablebg_without_datetime(self):
        soup = self.process()
        pagefooter = soup.find(id="pagefooter")

        self.assertIsNotNone(pagefooter)
        self.assertIsNotNone(pagefooter.find("table", class_="tablebg"))
        self.assertIsNone(pagefooter.find("p", class_="datetime"))
        self.assertNotIn("Powered by phpBB", pagefooter.get_text(" "))
        self.assertIn("Footer links", pagefooter.get_text(" "))


if __name__ == "__main__":
    unittest.main()
