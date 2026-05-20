"""Tests for spoiler onclick attribute preservation (issues #7 and #11)."""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import ForumParser, _SpoilerSafeFormatter, _fix_self_closing_spans
from bs4 import BeautifulSoup


SPOILER_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div class="quotecontent">
<span style="font-weight: bold"><span style="color: red">Содержимое спрятано под спойлер ↓</span></span>
<div style="padding: 3px; background-color: #FFFFFF; border: 1px solid #d8d8d8; font-size: 1em;">
<div style="text-transform: uppercase; border-bottom: 1px solid #CCCCCC; margin-bottom: 3px; font-size: 0.8em; font-weight: bold; display: block;">
<span onclick="if (this.parentNode.parentNode.getElementsByTagName('div')[1].getElementsByTagName('div')[0].style.display != '') { this.parentNode.parentNode.getElementsByTagName('div')[1].getElementsByTagName('div')[0].style.display = ''; this.innerHTML = '<b>Спойлер: </b><a href=\\'#\\' onClick=\\'return false;\\'>↓</a>'; } else { this.parentNode.parentNode.getElementsByTagName('div')[1].getElementsByTagName('div')[0].style.display = 'none'; this.innerHTML = '<b>Спойлер: </b><a href=\\'#\\' onClick=\\'return false;\\'>↕</a>'; }"><b>Спойлер: </b><a href="#" onclick="return false;">↕</a></span>
</div>
<div class="quotecontent"><div style="display: none;">spoiler content here</div></div>
</div>
</div>
</body>
</html>"""

# This is the exact HTML the live forum server sends — onclick uses &lt;/&gt; entities
# which BeautifulSoup decodes to < > during parsing.  Without our fix str(soup)
# would re-escape them back to &lt;/&gt;; with our formatter they survive as < >.
SPOILER_HTML_FROM_SERVER = """\
<!DOCTYPE html>
<html>
<body>
<div class="quotecontent"><span style="font-weight: bold"><span style="color: red">Содержимое спрятано под спойлер ↓</span></span><div style="padding: 3px; background-color: #FFFFFF; border: 1px solid #d8d8d8; font-size: 1em;"><div style="text-transform: uppercase; border-bottom: 1px solid #CCCCCC; margin-bottom: 3px; font-size: 0.8em; font-weight: bold; display: block;"><span onclick="if (this.parentNode.parentNode.getElementsByTagName('div')[1].getElementsByTagName('div')[0].style.display != '') {  this.parentNode.parentNode.getElementsByTagName('div')[1].getElementsByTagName('div')[0].style.display = ''; this.innerHTML = '&lt;b&gt;Спойлер: &lt;/b&gt;&lt;a href=\'#\' onClick=\'return false;\'&gt;↓&lt;/a&gt;'; } else { this.parentNode.parentNode.getElementsByTagName('div')[1].getElementsByTagName('div')[0].style.display = 'none'; this.innerHTML = '&lt;b&gt;Спойлер: &lt;/b&gt;&lt;a href=\'#\' onClick=\'return false;\'&gt;↕&lt;/a&gt;'; }"><b>Спойлер: </b><a href="#" onclick="return false;">↕</a></span></div>
<div class="quotecontent"><div style="display: none;">spoiler content here</div></div></div></div>
</body>
</html>"""


# Exact HTML from the live forum where the spoiler <span> is self-closing.
# Browsers ignore the slash on non-void elements and treat the following <b>/<a>
# as children of the span.  BeautifulSoup's html.parser honours the slash and
# produces an empty span, breaking the onclick label toggle (issue #11).
SPOILER_HTML_SELF_CLOSING_SPAN = """\
<!DOCTYPE html>
<html>
<body>
<div style="text-transform: uppercase; font-weight: bold; display: block;"><span onclick="if (this.parentNode.parentNode.getElementsByTagName('div')[1].getElementsByTagName('div')[0].style.display != '') { this.parentNode.parentNode.getElementsByTagName('div')[1].getElementsByTagName('div')[0].style.display = ''; this.innerHTML = '<b>Спойлер: </b><a href=\\'#\\' onClick=\\'return false;\\'>↓</a>'; } else { this.parentNode.parentNode.getElementsByTagName('div')[1].getElementsByTagName('div')[0].style.display = 'none'; this.innerHTML = '<b>Спойлер: </b><a href=\\'#\\' onClick=\\'return false;\\'>↕</a>'; }" /><b>Спойлер: </b><a href="#" onclick="return false;">↕</a></span></div>
<div class="quotecontent"><div style="display: none;">spoiler content here</div></div>
</body>
</html>"""


class TestFixSelfClosingSpans(unittest.TestCase):
    """Tests for _fix_self_closing_spans (issue #11)."""

    def test_self_closing_span_converted_to_open_tag(self):
        """<span ... /> must become <span ...> so its following siblings become children."""
        html = '<span onclick="test" />'
        result = _fix_self_closing_spans(html)
        # The trailing slash must be gone; a space before > is acceptable
        self.assertNotIn('/>', result, "Self-closing slash must be removed")
        self.assertTrue(result.startswith('<span'), f"Must still be a span tag: {result!r}")
        self.assertTrue(result.rstrip().endswith('>'), f"Must end with >: {result!r}")

    def test_span_with_onclick_containing_angle_brackets(self):
        """Self-closing span with < > inside onclick value must be fixed correctly."""
        html = """<span onclick="this.innerHTML = '<b>X</b>';" />"""
        result = _fix_self_closing_spans(html)
        self.assertTrue(result.endswith('>') and not result.endswith('/>'),
                        f"Expected open tag, got: {result!r}")
        self.assertIn("this.innerHTML = '<b>X</b>';", result)

    def test_void_elements_unchanged(self):
        """Void elements like <br />, <img />, <input /> must not be modified."""
        for tag in ('<br />', '<img src="x.png" />', '<input type="text" />'):
            self.assertEqual(_fix_self_closing_spans(tag), tag,
                             f"Void element must not be changed: {tag!r}")

    def test_normal_span_with_closing_tag_unchanged(self):
        """Regular <span>...</span> must pass through unchanged."""
        html = '<span class="foo"><b>bar</b></span>'
        self.assertEqual(_fix_self_closing_spans(html), html)

    def test_spoiler_span_children_preserved_after_fix(self):
        """After fixing the self-closing span, BeautifulSoup must see <b> and <a>
        as children of the span, not as siblings."""
        html = (
            '<span onclick="test" />'
            '<b>Спойлер: </b>'
            '<a href="#" onclick="return false;">↕</a>'
            '</span>'
        )
        fixed = _fix_self_closing_spans(html)
        soup = BeautifulSoup(fixed, "html.parser")
        span = soup.find('span')
        self.assertIsNotNone(span)
        children = list(span.children)
        tags = [c.name for c in children if hasattr(c, 'name') and c.name]
        self.assertIn('b', tags, "After fix, <b> must be a child of the span")
        self.assertIn('a', tags, "After fix, <a> must be a child of the span")

    def test_process_page_self_closing_span_spoiler(self):
        """process_page must produce a span that contains <b> and <a> as children
        so the spoiler label toggle works in the browser (issue #11)."""
        parser = ForumParser(output_dir="/tmp/test_parser_output")
        parser.download_image = MagicMock(return_value=None)
        parser.download_file = MagicMock(return_value=None)

        url = "https://visio.getbb.ru/viewtopic.php?t=1"
        result = parser.process_page(url, SPOILER_HTML_SELF_CLOSING_SPAN)

        soup = BeautifulSoup(result, "html.parser")
        span = soup.find('span', onclick=True)
        self.assertIsNotNone(span, "Spoiler span must be present in output")

        children = [c.name for c in span.children if hasattr(c, 'name') and c.name]
        self.assertIn('b', children,
                      "<b>Спойлер:</b> must be a child of the onclick span, not a sibling")
        self.assertIn('a', children,
                      "<a> arrow link must be a child of the onclick span, not a sibling")


class TestSpoilerFormatter(unittest.TestCase):
    def test_formatter_preserves_angle_brackets_in_attributes(self):
        """_SpoilerSafeFormatter must NOT escape < and > inside HTML attributes."""
        fmt = _SpoilerSafeFormatter()
        self.assertEqual(fmt.attribute_value("<b>test</b>"), "<b>test</b>")

    def test_formatter_still_escapes_ampersand_in_attributes(self):
        fmt = _SpoilerSafeFormatter()
        self.assertEqual(fmt.attribute_value("a & b"), "a &amp; b")

    def test_formatter_still_escapes_double_quote_in_attributes(self):
        fmt = _SpoilerSafeFormatter()
        self.assertEqual(fmt.attribute_value('say "hi"'), "say &quot;hi&quot;")

    def test_spoiler_onclick_not_escaped_after_roundtrip(self):
        """After parsing and re-serialising, onclick must still contain raw < > so the
        browser can execute the JavaScript that sets innerHTML."""
        soup = BeautifulSoup(SPOILER_HTML, "html.parser")
        out = soup.decode(formatter=_SpoilerSafeFormatter())

        # The onclick JS sets innerHTML to an HTML string; < and > must survive intact
        self.assertIn("this.innerHTML = '<b>Спойлер:", out,
                      "onclick innerHTML assignment must contain raw < not &lt;")
        self.assertNotIn("&lt;b&gt;Спойлер:", out,
                         "onclick must not contain escaped &lt;b&gt; which breaks the JS")

    def test_spoiler_onclick_preserved_via_process_page(self):
        """ForumParser.process_page must preserve onclick < > for spoiler functionality."""
        parser = ForumParser(output_dir="/tmp/test_parser_output")

        # Patch download methods to avoid network calls
        parser.download_image = MagicMock(return_value=None)
        parser.download_file = MagicMock(return_value=None)

        url = "https://visio.getbb.ru/viewtopic.php?t=1"
        result = parser.process_page(url, SPOILER_HTML)

        self.assertIn("this.innerHTML = '<b>Спойлер:", result,
                      "process_page output must contain raw < in onclick innerHTML")
        self.assertNotIn("&lt;b&gt;Спойлер:", result,
                         "process_page must not escape onclick content to &lt;b&gt;")

    def test_spoiler_onclick_entity_encoded_server_html(self):
        """process_page must handle the actual server HTML where onclick uses &lt;/&gt; entities.

        The live forum sends onclick attributes with &lt;b&gt; etc. inside innerHTML
        assignments.  BeautifulSoup decodes those entities to < > during parsing; our
        formatter must keep them as < > in the output, not re-escape them to &lt;/&gt;.
        """
        parser = ForumParser(output_dir="/tmp/test_parser_output")
        parser.download_image = MagicMock(return_value=None)
        parser.download_file = MagicMock(return_value=None)

        url = "https://visio.getbb.ru/viewtopic.php?t=1"
        result = parser.process_page(url, SPOILER_HTML_FROM_SERVER)

        self.assertIn("this.innerHTML = '<b>Спойлер:", result,
                      "onclick innerHTML must contain raw < after processing entity-encoded server HTML")
        self.assertNotIn("&lt;b&gt;Спойлер:", result,
                         "onclick must not have &lt;b&gt; after processing entity-encoded server HTML")

    def test_regular_content_ampersand_escaped(self):
        """Regular & in href attributes must still be escaped to &amp;."""
        html = '<html><body><a href="?a=1&amp;b=2">link</a></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        out = soup.decode(formatter=_SpoilerSafeFormatter())
        self.assertIn("&amp;", out, "& in href must be escaped to &amp;")


if __name__ == "__main__":
    unittest.main()
