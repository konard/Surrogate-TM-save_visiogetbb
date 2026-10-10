"""Owner feedback from 9 October on PR #18 (issue #17).

Markup is trimmed from pages the owner attached to the PR: index.html,
viewforum__f=2.html and viewtopic__f=3&t=1571.html.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from bs4 import BeautifulSoup

from parser import BASE_URL, ForumParser, _build_forum_structure, _remove_forum_chrome, url_to_local_path


INDEX = """<html><body><div id="wrapcentre">
<br style="clear: both;"/>
<table cellpadding="0" cellspacing="1" class="tablebg" style="margin-top: 5px;" width="100%">
<tr><td class="row1">
<p class="breadcrumbs"><a href="portal.html">Портал</a> » <a href="index.html">Список форумов</a></p>
<p class="datetime">Часовой пояс: UTC + 3 часа [ Летнее время ]</p>
</td></tr></table>
<br/>
<table cellspacing="1" class="tablebg" width="100%">
<tr><td align="right" class="cat" colspan="5"> </td></tr>
<tr><th colspan="2"> Форум </th><th width="50"> Темы </th><th width="50"> Сообщений </th><th> Последнее сообщение </th></tr>
<tr><td class="cat" colspan="2"><h4><a href="viewforum__f=1.html">Форум пользователей</a></h4></td><td class="catdiv" colspan="3"> </td></tr>
<tr>
<td align="center" class="row1" width="50"><img alt="Нет новых сообщений" src="styles/subsilver2-modded/imageset/forum_read_subforum.gif"/></td>
<td class="row1" width="100%"><div style="float: left;">
<a class="forumlink" href="viewforum__f=2.html">Версии и варианты поставки Visio</a>
<p class="forumdesc">Обсуждение версий Visio</p>
<p class="forumdesc"><strong>Подфорум: </strong> <a class="subforum read" href="viewforum__f=28.html">Публикации блога</a></p>
</div></td>
<td align="center" class="row2"><p class="topicdetails">93</p></td>
<td align="center" class="row2"><p class="topicdetails">1000</p></td>
<td align="center" class="row2" nowrap="nowrap"><p class="topicdetails"><a href="viewtopic__f=2&amp;t=657.html#p19217">Re: риббон</a></p></td>
</tr>
</table>
<span class="gensmall"><a href="#">Удалить cookies конференции</a> | <a href="#">Наша команда</a></span><br/>
<br clear="all"/>
<table cellpadding="0" cellspacing="1" class="tablebg" style="margin-top: 5px;" width="100%">
<tr><td class="row1">
<p class="breadcrumbs"><a href="portal.html">Портал</a> » <a href="index.html">Список форумов</a></p>
<p class="datetime">Часовой пояс: UTC + 3 часа [ Летнее время ]</p>
</td></tr></table>
<br clear="all"/>
<table cellspacing="1" class="tablebg" width="100%">
<tr><td class="cat" colspan="2"><h4>Кто сегодня был на конференции</h4></td></tr>
<tr><td class="row1"><img alt="Кто сейчас на конференции" src="styles/subsilver2-modded/theme/images/whosonline.gif"/></td>
<td class="row1"><span class="genmed">Сегодня на конференции было посетителей: <strong>11935</strong></span></td></tr>
</table>
<br clear="all"/>
<table cellspacing="1" class="tablebg" width="100%">
<tr><td class="cat" colspan="2"><h4>Статистика</h4></td></tr>
<tr><td class="row1"><img alt="Статистика" src="styles/subsilver2-modded/theme/images/whosonline.gif"/></td>
<td class="row1"><p class="genmed">Всего сообщений: <strong>18168</strong> | Тем: <strong>1577</strong></p></td></tr>
</table>
<br clear="all"/>
<form action="./ucp.php?mode=login&amp;sid=52071491a5e2e961475b7d038309d2fc" method="post">
<table cellspacing="1" class="tablebg" width="100%">
<tr><td class="cat"><h4><a href="#">Вход</a></h4></td></tr>
<tr><td align="center" class="row1"><span class="genmed">Имя пользователя:</span> <input class="post" name="username" type="text"/>
<span class="genmed">Пароль:</span> <input class="post" name="password" type="password"/>
<input class="btnmain" name="login" type="submit" value="Вход"/></td></tr>
</table>
</form>
<br/>
<img alt="cron" height="1" src="./cron.php?cron_type=tidy_sessions" width="1"/></div>
</body></html>"""

FORUM = """<html><body><div id="wrapcentre">
<table class="tablebg"><tr><td class="row1">
<p class="breadcrumbs"><a href="index.html">Список форумов</a> » <a href="viewforum__f=2.html">Версии и варианты поставки Visio</a></p>
<p class="datetime">Часовой пояс: UTC + 3 часа [ Летнее время ]</p>
</td></tr></table>
<div id="pagecontent">
<table cellspacing="1" class="tablebg" width="100%">
<tr><th colspan="3"> Темы </th><th> Автор </th><th> Ответы </th><th> Просмотры </th><th> Последнее сообщение </th></tr>
<tr>
<td class="row1"></td><td class="row1"></td>
<td class="row1"><a class="topictitle" href="viewtopic__f=2&amp;t=657.html">Создание пользовательского риббона</a></td>
<td class="row2"><p class="topicauthor">Surrogate</p></td>
<td class="row1"><p class="topicdetails">18</p></td>
<td class="row2"><p class="topicdetails">8196</p></td>
<td class="row1"><p class="topicdetails">Surrogate <a href="viewtopic__f=2&amp;t=657.html#p19217">последнее</a></p></td>
</tr>
</table>
</div>
<table class="tablebg"><tr><td class="row1">
<p class="breadcrumbs"><a href="index.html">Список форумов</a> » <a href="viewforum__f=2.html">Версии и варианты поставки Visio</a></p>
<p class="datetime">Часовой пояс: UTC + 3 часа [ Летнее время ]</p>
</td></tr></table>
</div></body></html>"""

TOPIC = """<html><body><div id="wrapcentre"><div id="pagecontent">
<table cellspacing="1" class="tablebg" width="100%">
<tr class="row1">
<td class="profile" valign="top"><a name="p19217"></a><b class="postauthor">Surrogate</b></td>
<td valign="top" width="100%"><div class="postbody">Текст сообщения</div></td>
</tr>
<tr class="row1">
<td class="profile"><strong><a href="viewtopic__f=3&amp;t=1571.html">Вернуться к началу</a></strong></td>
<td><div class="gensmall" style="float: left;">&nbsp;<a href="./memberlist.php?mode=viewprofile&amp;u=2"><img alt="Профиль" src="./styles/subsilver2-modded/imageset/ru/icon_user_profile.gif" title="Профиль"/></a>&nbsp;</div> <div class="gensmall" style="float: right;"><a href="./posting.php?mode=quote&amp;f=3&amp;p=19217"><img alt="Ответить с цитатой" src="./styles/subsilver2-modded/imageset/ru/icon_post_quote.gif" title="Ответить с цитатой"/></a>&nbsp;</div></td>
</tr>
</table>
</div></div></body></html>"""

# The same post footer cell as saved by version 2026.10.08.
SAVED_POST_FOOTER = """<div id="pagecontent"><table><tr class="row1">
<td class="profile"><strong><a href="viewtopic__f=3&amp;t=1571.html">Вернуться к началу</a></strong></td>
<td><div class="gensmall" style="float: left;"> <img alt="Профиль" src="styles/subsilver2-modded/imageset/ru/icon_user_profile.gif" title="Профиль"/>  </div> <div class="gensmall" style="float: right;"><a href="#"><img alt="Ответить с цитатой" src="styles/subsilver2-modded/imageset/ru/icon_post_quote.gif" title="Ответить с цитатой"/></a>  </div></td>
</tr></table></div>"""


def clean(html):
    soup = BeautifulSoup(html, "html.parser")
    _remove_forum_chrome(soup)
    return soup


def response(body, content_type="text/html; charset=utf-8"):
    result = requests.Response()
    result.status_code = 200
    result.headers["Content-Type"] = content_type
    result._content = body.encode()
    result.encoding = "utf-8"
    return result


class TestTimeZoneRemoved(unittest.TestCase):
    def test_time_zone_removed_from_top_and_bottom_breadcrumbs(self):
        for html in (INDEX, FORUM):
            soup = clean(html)
            self.assertNotIn("Часовой пояс", soup.get_text())
            self.assertIsNone(soup.find(class_="datetime"))

    def test_forum_breadcrumbs_are_kept(self):
        soup = clean(FORUM)
        self.assertEqual(len(soup.find_all(class_="breadcrumbs")), 2)


class TestIndexFooterRemoved(unittest.TestCase):
    def setUp(self):
        self.soup = clean(INDEX)
        self.text = self.soup.get_text(" ", strip=True)

    def test_footer_blocks_removed(self):
        for label in ("Удалить cookies конференции", "Наша команда",
                      "Кто сегодня был на конференции", "Статистика",
                      "Всего сообщений", "Имя пользователя", "Пароль"):
            self.assertNotIn(label, self.text)
        self.assertIsNone(self.soup.find("form"))
        self.assertIsNone(self.soup.find("input"))

    def test_cron_beacon_removed(self):
        self.assertNotIn("cron.php", str(self.soup))

    def test_only_top_breadcrumbs_remain(self):
        self.assertEqual(len(self.soup.find_all(class_="breadcrumbs")), 1)
        self.assertIn("Список форумов", self.text)

    def test_forum_list_kept(self):
        self.assertIsNotNone(self.soup.find("a", class_="forumlink"))
        self.assertIn("Форум пользователей", self.text)
        self.assertIn("Последнее сообщение", self.text)

    def test_cron_beacon_is_not_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = ForumParser(tmp, delay=0)
            parser.download_image = MagicMock(return_value=None)
            parser.process_page(BASE_URL + "/index.php", INDEX)
            requested = [call.args[0] for call in parser.download_image.call_args_list]
            self.assertFalse([url for url in requested if "cron.php" in url])


class TestPostFooterIconsRemoved(unittest.TestCase):
    def assert_cleaned(self, soup):
        self.assertIsNone(soup.find("img", alt="Профиль"))
        self.assertIsNone(soup.find("img", alt="Ответить с цитатой"))
        self.assertIsNotNone(soup.find("a", string="Вернуться к началу"))

    def test_profile_and_quote_icons_removed_from_fresh_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = ForumParser(tmp, delay=0)
            parser.download_image = MagicMock(return_value=None)
            soup = BeautifulSoup(parser.process_page(BASE_URL + "/viewtopic.php?f=3&t=1571", TOPIC),
                                 "html.parser")
        self.assert_cleaned(soup)
        self.assertIn("Текст сообщения", soup.get_text())

    def test_profile_and_quote_icons_removed_from_saved_page(self):
        self.assert_cleaned(clean(SAVED_POST_FOOTER))

    def test_icons_inside_post_text_are_kept(self):
        soup = clean('<div id="pagecontent"><table><tr><td><div class="postbody">'
                     '<img alt="Профиль" src="./styles/x/imageset/ru/icon_user_profile.gif"/>'
                     '</div></td></tr></table></div>')
        self.assertIsNotNone(soup.find("img", alt="Профиль"))


class TestForumJsonFromSavedPages(unittest.TestCase):
    def test_sections_threads_and_posts_from_local_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "index.html").write_text(str(clean(INDEX)), encoding="utf-8")
            (output / "viewforum__f=2.html").write_text(str(clean(FORUM)), encoding="utf-8")
            (output / "viewtopic__f=2&t=657.html").write_text(TOPIC, encoding="utf-8")
            structure = _build_forum_structure(output)
        self.assertEqual([s["title"] for s in structure["sections"]], ["Форум пользователей"])
        subsection = structure["sections"][0]["subsections"][0]
        self.assertEqual(subsection["title"], "Версии и варианты поставки Visio")
        self.assertEqual(subsection["url"], "viewforum__f=2.html")
        self.assertEqual(subsection["description"], "Обсуждение версий Visio")
        self.assertEqual([t["title"] for t in subsection["threads"]], ["Создание пользовательского риббона"])
        self.assertEqual(subsection["threads"][0]["posts"][0]["text"], "Текст сообщения")


class TestResumeRetriesFailedAttachments(unittest.TestCase):
    PAGE = BASE_URL + "/viewtopic.php?f=29&t=1211&start=80"
    HTML = ('<div id="pagecontent"><a name="p11583"></a><div class="postbody">'
            '<a href="./download/file.php?id=811">scheme.vsd</a>'
            '<img src="./download/file.php?id=861"/></div></div>')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name)

    def crawl(self, resume, side_effect):
        parser = ForumParser(self.output, delay=0, resume=resume)
        parser.session.get = MagicMock(side_effect=side_effect)
        try:
            with patch("parser.time.sleep"):
                parser.crawl(self.PAGE)
        finally:
            parser._download_log_handler.close()
        return parser

    def saved(self):
        return BeautifulSoup(url_to_local_path(self.PAGE, self.output).read_text(encoding="utf-8"),
                             "html.parser")

    def test_failed_attachment_links_to_forum_until_downloaded(self):
        self.crawl(False, [response(self.HTML)] + [requests.ReadTimeout("outage")] * 6)
        soup = self.saved()
        self.assertEqual(soup.find("a", string="scheme.vsd")["href"],
                         BASE_URL + "/download/file.php?id=811")
        self.assertEqual(soup.img["src"], BASE_URL + "/download/file.php?id=861")

    def test_resume_downloads_attachments_of_saved_page(self):
        self.crawl(False, [response(self.HTML)] + [requests.ReadTimeout("outage")] * 6)
        vsd = response("vsd", "application/vnd.visio")
        png = response("png", "image/png")
        parser = self.crawl(True, [vsd, png])
        soup = self.saved()
        self.assertEqual(soup.find("a", string="scheme.vsd")["href"], "download/file_811.vsd")
        self.assertEqual(soup.img["src"], "download/file_861.png")
        self.assertEqual(parser.session.get.call_count, 2)
        report = (self.output / "failed_attachments.md").read_text(encoding="utf-8")
        self.assertNotIn("file.php", report)

    def test_resume_reports_attachments_that_still_fail(self):
        self.crawl(False, [response(self.HTML)] + [requests.ReadTimeout("outage")] * 6)
        self.crawl(True, [requests.ReadTimeout("outage")] * 6)
        report = (self.output / "failed_attachments.md").read_text(encoding="utf-8")
        self.assertIn(f"| {BASE_URL}/viewtopic.php?p=11583#p11583 | {BASE_URL}/download/file.php?id=811 |",
                      report)
        self.assertIn("file.php?id=861", report)

    def test_offline_repair_points_dead_attachment_links_to_forum(self):
        path = url_to_local_path(self.PAGE, self.output)
        path.write_text(self.HTML, encoding="utf-8")
        parser = ForumParser(self.output, delay=0)
        parser.session.get = MagicMock(side_effect=AssertionError("repair must stay offline"))
        parser.repair_archive()
        soup = self.saved()
        self.assertEqual(soup.find("a", string="scheme.vsd")["href"],
                         BASE_URL + "/download/file.php?id=811")
        self.assertEqual(soup.img["src"], BASE_URL + "/download/file.php?id=861")


if __name__ == "__main__":
    unittest.main()
