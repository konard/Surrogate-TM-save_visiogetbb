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
from parser import ForumParser, _is_incomplete_topic_page, _remove_forum_chrome, url_to_local_path, normalize_url


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

VIEWFORUM_FOOTER_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div id="pagecontent">
  <table cellspacing="1" width="100%"><tr><td>Forum topics list</td></tr></table>
</div>
<div id="pagefooter">
  <table cellspacing="1" class="tablebg" width="100%">
    <tr><td class="cat"><h4>Кто сейчас на конференции</h4></td></tr>
    <tr><td class="row1"><p class="gensmall">Сейчас этот форум просматривают: нет зарегистрированных пользователей и гости: 6</p></td></tr>
  </table>
  <br clear="all"/>
  <table cellspacing="0" width="100%">
    <tr>
      <td align="left" valign="top"><table border="0" cellpadding="0" cellspacing="3"><tr>
        <td class="gensmall">Новые сообщения</td>
      </tr></table></td>
      <td align="right"><span class="gensmall">Вы <strong>можете</strong> начинать темы</span></td>
    </tr>
  </table>
  <br clear="all"/>
  <table cellspacing="0" width="100%">
    <tr>
      <td><form action="./search.php" method="post" name="search"><span class="gensmall">Найти:</span></form></td>
      <td align="right"><form action="./viewforum.php" method="post" name="jumpbox"><select name="f"><option value="-1">Выберите форум</option></select></form></td>
    </tr>
  </table>
</div>
<div id="wrapfooter">
  <span class="copyright">Powered by <a href="http://www.phpbb.com/">phpBB</a></span>
</div>
<!--LiveInternet counter--><script type="text/javascript"><!--
new Image().src = "http://counter.yadro.ru/hit;getbb?r"+escape(document.referrer)+";";//--></script><!--/LiveInternet-->
</body>
</html>"""

EMPTY_SAVED_TOPIC_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div id="pageheader">
<h2><a class="titles" href="viewtopic__f=29&amp;t=236.html">Новые BBCode на форуме</a></h2>
</div>
<div id="pagecontent">
<table cellspacing="1" width="100%">
<tr>
<td class="nav" nowrap="nowrap" valign="middle"> Страница <strong>1</strong> из <strong>1</strong><br/></td>
<td class="gensmall" nowrap="nowrap"> [ Сообщений: 7 ] </td>
<td align="right" class="gensmall" nowrap="nowrap" width="100%"></td>
</tr>
</table>
<table cellspacing="1" width="100%">
<tr>
<td class="nav" nowrap="nowrap" valign="middle"> Страница <strong>1</strong> из <strong>1</strong><br/></td>
<td class="gensmall" nowrap="nowrap"> [ Сообщений: 7 ] </td>
<td align="right" class="gensmall" nowrap="nowrap" width="100%"></td>
</tr>
</table>
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
        self.assertIsNone(soup.find(id="pagefooter"))
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

    def test_pagefooter_removed_entirely(self):
        soup = self.process()

        self.assertIsNone(soup.find(id="pagefooter"))
        self.assertIsNone(soup.find(id="wrapfooter"))
        self.assertIsNone(soup.find("p", class_="datetime"))

    def test_removes_topic_print_previous_and_next_navigation(self):
        html = """<html><body><div id="pagecontent"><table><tr>
        <td class="nav"><a href="./viewtopic.php?f=2&amp;t=40&amp;view=print">Для печати</a></td>
        <td class="nav"><a href="./viewtopic.php?f=2&amp;t=40&amp;view=previous">Пред. тема</a> |
        <a href="./viewtopic.php?f=2&amp;t=40&amp;view=next">След. тема</a></td>
        </tr></table><div class="postbody">Useful post</div></div></body></html>"""

        result = self.parser.process_page(
            "https://visio.getbb.ru/viewtopic.php?f=2&t=40", html
        )
        soup = BeautifulSoup(result, "html.parser")

        self.assertNotIn("Для печати", soup.get_text(" "))
        self.assertNotIn("Пред. тема", soup.get_text(" "))
        self.assertNotIn("След. тема", soup.get_text(" "))
        self.assertIn("Useful post", soup.get_text(" "))

    def test_removes_topic_permissions_footer_table(self):
        html = """<html><body><div id="pagecontent"><div class="postbody">Useful post</div></div>
        <table cellspacing="1" width="100%"><tr><td></td><td><span class="gensmall">
        Вы <strong>не можете</strong> начинать темы<br/>Вы <strong>не можете</strong> отвечать на сообщения<br/>
        Вы <strong>не можете</strong> добавлять вложения</span></td></tr></table></body></html>"""

        result = self.parser.process_page(
            "https://visio.getbb.ru/viewtopic.php?f=2&t=40", html
        )
        soup = BeautifulSoup(result, "html.parser")

        self.assertNotIn("не можете", soup.get_text(" "))
        self.assertIn("Useful post", soup.get_text(" "))

    def test_viewforum_footer_junk_removed(self):
        result = self.parser.process_page(
            "https://visio.getbb.ru/viewforum.php?f=29",
            VIEWFORUM_FOOTER_HTML,
        )
        soup = BeautifulSoup(result, "html.parser")

        self.assertIsNone(soup.find(id="pagefooter"))
        self.assertIsNone(soup.find(id="wrapfooter"))
        self.assertIsNone(soup.find("script", string=lambda t: t and "counter.yadro.ru" in t))
        self.assertNotIn("Кто сейчас на конференции", soup.get_text(" "))
        self.assertNotIn("Powered by", soup.get_text(" "))
        self.assertIn("Forum topics list", soup.get_text(" "))

    def test_resume_skips_existing_page(self):
        self.parser.resume = True
        url = "https://visio.getbb.ru/viewforum.php?f=1"
        local_path = url_to_local_path(normalize_url(url), Path(self.tempdir.name))
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text("<html>cached</html>", encoding="utf-8")

        self.parser.fetch = MagicMock()
        self.parser.save_page(url)

        self.parser.fetch.assert_not_called()
        self.assertEqual(self.parser.pages_saved, 1)

    def test_no_resume_refetches_existing_page(self):
        self.parser.resume = False
        url = "https://visio.getbb.ru/viewforum.php?f=1"
        local_path = url_to_local_path(normalize_url(url), Path(self.tempdir.name))
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text("<html>cached</html>", encoding="utf-8")

        response = MagicMock()
        response.headers = {"Content-Type": "text/html; charset=utf-8"}
        response.text = "<html><body><div id='pagecontent'>ok</div></body></html>"
        response.content = response.text.encode()
        self.parser.fetch = MagicMock(return_value=response)

        self.parser.save_page(url)

        self.parser.fetch.assert_called_once()

    def test_detects_incomplete_topic_page_before_saving(self):
        soup = BeautifulSoup(EMPTY_SAVED_TOPIC_HTML, "html.parser")

        self.assertTrue(
            _is_incomplete_topic_page(
                "https://visio.getbb.ru/viewtopic.php?f=29&t=236",
                soup,
            )
        )

    def test_save_page_does_not_write_topic_when_posts_are_missing(self):
        response = MagicMock()
        response.headers = {"Content-Type": "text/html; charset=utf-8"}
        response.text = EMPTY_SAVED_TOPIC_HTML
        response.content = EMPTY_SAVED_TOPIC_HTML.encode("utf-8")
        response.url = "https://visio.getbb.ru/viewtopic.php?f=29&t=236"
        self.parser.fetch = MagicMock(return_value=response)

        self.parser.save_page("https://visio.getbb.ru/viewtopic.php?f=29&t=236")

        self.assertEqual(self.parser.pages_saved, 0)
        self.assertFalse(
            (
                Path(self.tempdir.name)
                / "viewtopic__f=29&t=236.html"
            ).exists()
        )


class TestTopicUrlCanonicalization(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.parser = ForumParser(output_dir=self.tempdir.name)
        self.parser.download_image = MagicMock(return_value=None)
        self.parser.download_file = MagicMock(return_value=None)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_topic_variants_normalize_to_one_url(self):
        expected = "https://visio.getbb.ru/viewtopic.php?f=2&t=40"
        variants = [
            "https://visio.getbb.ru/viewtopic.php?f=2&t=40&p=181",
            "https://visio.getbb.ru/viewtopic.php?f=2&t=40&view=print",
        ]

        for variant in variants:
            with self.subTest(variant=variant):
                self.assertEqual(normalize_url(variant), expected)

    def test_post_permalink_becomes_anchor_and_is_not_enqueued(self):
        html = """<html><body><div id="pagecontent"><table><tr>
        <td><a href="./viewtopic.php?p=178#p178"><img alt="Сообщение" src="target.gif"/></a></td>
        </tr></table></div></body></html>"""

        result = self.parser.process_page(
            "https://visio.getbb.ru/viewtopic.php?f=2&t=40", html
        )
        soup = BeautifulSoup(result, "html.parser")

        self.assertEqual(soup.find("a")["href"], "#p178")
        self.assertEqual(list(self.parser.queue), [])

    def test_post_permalink_without_fragment_uses_post_id_anchor(self):
        html = '<html><body><a href="./viewtopic.php?f=2&amp;t=40&amp;p=181">quoted post</a></body></html>'

        result = self.parser.process_page(
            "https://visio.getbb.ru/viewtopic.php?f=2&t=40", html
        )
        soup = BeautifulSoup(result, "html.parser")

        self.assertEqual(soup.find("a")["href"], "#p181")


ORPHANED_FOOTER_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div id="pagecontent">
  <table cellspacing="1" width="100%"><tr><td>Forum topics list</td></tr></table>
</div>
<!-- Yandex.RTB R-A-1239576-1 -->
<br clear="all"/>
<table cellspacing="1" class="tablebg" width="100%">
<tr><td class="cat"><h4>Кто сейчас на конференции</h4></td></tr>
<tr><td class="row1"><p class="gensmall">Сейчас этот форум просматривают: гости: 11</p></td></tr>
</table>
<br clear="all"/>
<table cellspacing="0" width="100%">
<tr>
<td align="left" valign="top">
<table border="0" cellpadding="0" cellspacing="3"><tr>
<td class="gensmall">Новые сообщения</td>
<td>  </td>
<td class="gensmall">Нет новых сообщений</td>
</tr></table>
</td>
<td align="right"><span class="gensmall">Вы <strong>можете</strong> начинать темы</span></td>
</tr>
</table>
<br clear="all"/>
<table cellspacing="0" width="100%">
<tr>
<td><form action="./search.php" method="post" name="search"><span class="gensmall">Найти:</span> <input name="keywords" type="text"/> <input type="submit" value="Перейти"/></form></td>
<td align="right"><form action="./viewforum.php" method="post" name="jumpbox">
<select name="f" onchange="submit()"><option value="-1">Выберите форум</option></select>
<input type="submit" value="Перейти"/>
</form></td>
</tr>
</table>
<!--
We request you retain the full copyright notice below including the link to www.phpbb.com.
The phpBB Group : 2006
//-->
<!--LiveInternet counter--><script type="text/javascript">new Image().src = "http://counter.yadro.ru/hit";</script><!--/LiveInternet-->
</body>
</html>"""

PROFILE_LINKS_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div id="pagecontent">
  <table class="tablebg"><tr>
    <td class="postprofile">
      <a href="./memberlist.php?mode=viewprofile&amp;u=42">AuthorName</a>
    </td>
    <td class="postbody">Post content here.</td>
  </tr></table>
</div>
</body>
</html>"""

REPUTATION_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div id="pagecontent">
  <table class="tablebg"><tr>
    <td class="postprofile">
      <a href="./memberlist.php?mode=viewprofile&amp;u=42">AuthorName</a>
      <br/>
      <a href="./post_thanks.php?action=add&amp;post_id=123"><img src="plus.gif" alt="+"/></a>
      <a href="./post_thanks.php?action=remove&amp;post_id=123"><img src="minus.gif" alt="-"/></a>
      <span class="reputation-score">+5</span>
    </td>
    <td class="postbody">Useful post content.</td>
  </tr></table>
</div>
</body>
</html>"""


class TestOrphanedFooterJunkRemoval(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.parser = ForumParser(output_dir=self.tempdir.name)
        self.parser.download_image = MagicMock(return_value=None)
        self.parser.download_file = MagicMock(return_value=None)

    def tearDown(self):
        self.tempdir.cleanup()

    def process(self, html: str) -> BeautifulSoup:
        result = self.parser.process_page(
            "https://visio.getbb.ru/viewforum.php?f=29",
            html,
        )
        return BeautifulSoup(result, "html.parser")

    def test_orphaned_online_users_table_removed(self):
        soup = self.process(ORPHANED_FOOTER_HTML)
        self.assertNotIn("Кто сейчас на конференции", soup.get_text(" "))

    def test_orphaned_icon_legend_table_removed(self):
        soup = self.process(ORPHANED_FOOTER_HTML)
        self.assertNotIn("Новые сообщения", soup.get_text(" "))
        self.assertNotIn("Нет новых сообщений", soup.get_text(" "))

    def test_orphaned_permissions_table_removed(self):
        soup = self.process(ORPHANED_FOOTER_HTML)
        self.assertNotIn("можете начинать темы", soup.get_text(" "))

    def test_orphaned_search_jumpbox_forms_removed(self):
        soup = self.process(ORPHANED_FOOTER_HTML)
        self.assertIsNone(soup.find("form", attrs={"name": "search"}))
        self.assertIsNone(soup.find("form", attrs={"name": "jumpbox"}))

    def test_phpbb_copyright_comment_removed(self):
        result = self.parser.process_page(
            "https://visio.getbb.ru/viewforum.php?f=29",
            ORPHANED_FOOTER_HTML,
        )
        self.assertNotIn("phpBB Group", result)
        self.assertNotIn("www.phpbb.com", result)

    def test_liveinternet_comment_markers_removed(self):
        result = self.parser.process_page(
            "https://visio.getbb.ru/viewforum.php?f=29",
            ORPHANED_FOOTER_HTML,
        )
        self.assertNotIn("LiveInternet counter", result)
        self.assertNotIn("counter.yadro.ru", result)

    def test_pagecontent_preserved(self):
        soup = self.process(ORPHANED_FOOTER_HTML)
        self.assertIn("Forum topics list", soup.get_text(" "))


class TestProfileLinkUnlinking(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.parser = ForumParser(output_dir=self.tempdir.name)
        self.parser.download_image = MagicMock(return_value=None)
        self.parser.download_file = MagicMock(return_value=None)

    def tearDown(self):
        self.tempdir.cleanup()

    def process(self, html: str) -> BeautifulSoup:
        result = self.parser.process_page(
            "https://visio.getbb.ru/viewtopic.php?f=29&t=100",
            html,
        )
        return BeautifulSoup(result, "html.parser")

    def test_profile_link_replaced_with_text(self):
        soup = self.process(PROFILE_LINKS_HTML)
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            self.assertNotIn("viewprofile", href, f"Profile link not removed: {href}")

    def test_author_name_still_visible(self):
        soup = self.process(PROFILE_LINKS_HTML)
        self.assertIn("AuthorName", soup.get_text(" "))

    def test_post_content_preserved(self):
        soup = self.process(PROFILE_LINKS_HTML)
        self.assertIn("Post content here", soup.get_text(" "))


class TestReputationElementRemoval(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.parser = ForumParser(output_dir=self.tempdir.name)
        self.parser.download_image = MagicMock(return_value=None)
        self.parser.download_file = MagicMock(return_value=None)

    def tearDown(self):
        self.tempdir.cleanup()

    def process(self, html: str) -> BeautifulSoup:
        result = self.parser.process_page(
            "https://visio.getbb.ru/viewtopic.php?f=29&t=100",
            html,
        )
        return BeautifulSoup(result, "html.parser")

    def test_reputation_links_removed(self):
        soup = self.process(REPUTATION_HTML)
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            self.assertNotIn("post_thanks", href, f"Reputation link not removed: {href}")

    def test_post_content_preserved_after_reputation_removal(self):
        soup = self.process(REPUTATION_HTML)
        self.assertIn("Useful post content", soup.get_text(" "))


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.parser = ForumParser(output_dir=self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_fetch_returns_response_on_success(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        self.parser.session.get = MagicMock(return_value=resp)

        result = self.parser.fetch("https://example.com/file")
        self.assertIsNotNone(result)
        self.parser.session.get.assert_called_once()

    def test_fetch_returns_none_on_failure(self):
        import requests as req

        self.parser.session.get = MagicMock(side_effect=req.exceptions.ConnectionError("refused"))
        result = self.parser.fetch("https://example.com/file")
        self.assertIsNone(result)
        self.parser.session.get.assert_called_once()


class TestDownloadLog(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tempdir.name)
        self.parser = ForumParser(output_dir=self.tempdir.name)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.parser._init_download_log()

    def tearDown(self):
        # Close the file handler before cleanup to avoid Windows file lock issues
        if self.parser._download_log_handler:
            self.parser._download_log_handler.close()
        self.tempdir.cleanup()

    def _read_log(self) -> str:
        log_path = self.output_dir / "downloads.log"
        if not log_path.exists():
            return ""
        with open(log_path, encoding="utf-8") as f:
            return f.read()

    def test_download_log_created_after_init(self):
        self.assertTrue((self.output_dir / "downloads.log").exists())

    def test_successful_download_logged_ok(self):
        fake_path = self.output_dir / "file_42.pdf"
        self.parser._log_download_ok("https://example.com/download/file.php?id=42", fake_path)
        log_contents = self._read_log()
        self.assertIn("OK", log_contents)
        self.assertIn("file.php?id=42", log_contents)

    def test_failed_download_logged_fail(self):
        self.parser._log_download_fail("https://example.com/download/file.php?id=99", "fetch failed")
        log_contents = self._read_log()
        self.assertIn("FAIL", log_contents)
        self.assertIn("file.php?id=99", log_contents)


INDEX_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div id="pagecontent">
<table>
<tr><td class="cat"><strong>Раздел 1</strong></td></tr>
<tr>
  <td class="row1">
    <a href="./viewforum.php?f=5">Подраздел А</a>
    <span class="genmed">Описание подраздела А</span>
  </td>
</tr>
</table>
</div>
</body>
</html>"""

FORUM_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div id="pagecontent">
<table>
<tr><td class="cat">Темы</td></tr>
<tr>
  <td class="row1">
    <a href="./viewtopic.php?f=5&amp;t=10">Первая тема</a>
  </td>
</tr>
</table>
</div>
</body>
</html>"""

TOPIC_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div id="pagecontent">
<table class="tablebg">
<tr>
  <td class="postprofile">AuthorName</td>
  <td class="postbody">Текст первого поста.</td>
</tr>
</table>
</div>
</body>
</html>"""


class TestJsonExport(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _write(self, filename: str, content: str) -> None:
        path = self.output_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_json_export_created_by_crawl(self):
        from unittest.mock import patch

        parser_instance = ForumParser(output_dir=self.tempdir.name)
        self._write("index.html", INDEX_HTML)

        with patch.object(parser_instance, "crawl", wraps=lambda start_url=None: parser_instance._export_json()):
            parser_instance._export_json()

        json_path = self.output_dir / "forum.json"
        self.assertTrue(json_path.exists(), "forum.json should be created")

    def test_json_contains_sections(self):
        from parser import _build_forum_structure
        self._write("index.html", INDEX_HTML)
        structure = _build_forum_structure(self.output_dir)
        self.assertIn("sections", structure)

    def test_json_sections_have_subsections(self):
        from parser import _build_forum_structure
        self._write("index.html", INDEX_HTML)
        structure = _build_forum_structure(self.output_dir)
        sections = structure.get("sections", [])
        self.assertTrue(len(sections) > 0, "Should have at least one section")

    def test_json_threads_extracted_from_saved_forum_page(self):
        from parser import _build_forum_structure
        self._write("index.html", INDEX_HTML)
        self._write("viewforum__f=5.html", FORUM_HTML)
        structure = _build_forum_structure(self.output_dir)
        sections = structure.get("sections", [])
        all_threads = [
            t
            for sec in sections
            for sub in sec.get("subsections", [])
            for t in sub.get("threads", [])
        ]
        self.assertTrue(len(all_threads) > 0, "Should find at least one thread")
        self.assertIn("Первая тема", all_threads[0]["title"])

    def test_json_posts_extracted_from_saved_topic_page(self):
        from parser import _build_forum_structure
        self._write("index.html", INDEX_HTML)
        self._write("viewforum__f=5.html", FORUM_HTML)
        self._write("viewtopic__f=5&t=10.html", TOPIC_HTML)
        structure = _build_forum_structure(self.output_dir)
        all_posts = [
            p
            for sec in structure.get("sections", [])
            for sub in sec.get("subsections", [])
            for t in sub.get("threads", [])
            for p in t.get("posts", [])
        ]
        self.assertTrue(len(all_posts) > 0, "Should find at least one post")
        self.assertIn("Текст первого поста", all_posts[0]["text"])


if __name__ == "__main__":
    unittest.main()
