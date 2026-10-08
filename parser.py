#!/usr/bin/env python3
"""
Forum parser for visio.getbb.ru
Saves a full local copy of the forum with:
- Navigation between sections and threads
- Embedded images and file attachments (with proper extensions)
- Pagination mirroring the original forum
- Working local hyperlinks
Excludes: viewprofile, search.php, ucp.php
"""

import json
import os
import re
import sys
import time
import mimetypes
import argparse
import logging
import urllib.parse
from pathlib import Path
from collections import deque
from urllib.parse import urlparse, urljoin, urlunparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup, NavigableString
from bs4.formatter import HTMLFormatter

BASE_URL = "https://visio.getbb.ru"
PARSER_VERSION = "2026.10.08"

# Total attempts per attachment over the whole run, deferred passes included.
ATTACHMENT_ATTEMPTS = 3
ATTACHMENT_RETRY_DELAY = 1.0

# Bound stalled requests; a timeout alone does not distinguish server outages,
# rate limits and network failures. The old timeout cost a minute per failure.
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 20.0

# When the server starts stalling requests, hammering it keeps the throttle
# alive. After this many consecutive transient failures, cool down with an
# exponential (capped) pause so the rate limit can expire.
THROTTLE_TRIGGER = 3
THROTTLE_BACKOFF_BASE = 5.0
THROTTLE_BACKOFF_MAX = 30.0
# Defer requests to an unavailable host after this many consecutive failures.
# Each deferred pass starts a fresh, bounded set of probes.
HOST_FAILURE_LIMIT = 6

# Number of deferred passes over transiently-failed downloads after the crawl.
RETRY_PASSES = 2
RETRY_PASS_PAUSE = 30.0

# Markdown table of attachments that could not be downloaded (issue #17).
FAILED_ATTACHMENTS_FILE = "failed_attachments.md"

# BeautifulSoup's default formatter escapes < and > inside attribute values (e.g. onclick),
# which breaks spoiler expand/collapse handlers that use innerHTML with HTML markup.
# This formatter only escapes & and " in attributes, preserving < and > for JavaScript.
class _SpoilerSafeFormatter(HTMLFormatter):
    def attribute_value(self, value: str) -> str:
        return value.replace("&", "&amp;").replace('"', "&quot;")


def _fix_self_closing_spans(html: str) -> str:
    """Convert self-closing <span .../> tags to regular open tags <span ...>.

    The forum server sometimes emits <span onClick="..." /> (self-closing).
    Browsers ignore the slash on non-void elements and treat the following
    sibling nodes as children of the span.  BeautifulSoup's html.parser
    honours the slash and creates an *empty* span instead, breaking the
    spoiler onclick handler which relies on ``this`` containing the visible
    label text.  Stripping the slash before parsing restores browser behaviour.
    """
    result: list[str] = []
    i = 0
    length = len(html)
    while i < length:
        # Look for the start of a <span tag (case-insensitive)
        if html[i] == '<' and html[i+1:i+5].lower() == 'span' and (
            i + 5 >= length or not html[i+5].isalnum()
        ):
            j = i + 5
            in_quote: str | None = None
            while j < length:
                c = html[j]
                if in_quote:
                    if c == in_quote:
                        in_quote = None
                elif c in ('"', "'"):
                    in_quote = c
                elif c == '>':
                    # End of tag — strip trailing slash if self-closing
                    if j > 0 and html[j - 1] == '/':
                        result.append(html[i:j - 1])
                        result.append('>')
                    else:
                        result.append(html[i:j + 1])
                    i = j + 1
                    break
                j += 1
            else:
                # Reached end of string without finding >
                result.append(html[i:])
                i = length
        else:
            result.append(html[i])
            i += 1
    return ''.join(result)


def _remove_forum_chrome(soup: BeautifulSoup) -> None:
    """Remove navigation and form controls that are useless in a static archive."""
    from bs4 import Comment

    for selector in ("#menubar", "#datebar", "p.searchbar"):
        for tag in soup.select(selector):
            tag.decompose()

    pagecontent = soup.find(id="pagecontent")
    if pagecontent:
        for table in pagecontent.select("table.tablebg"):
            if table.find("form", attrs={"name": "viewtopic"}):
                table.decompose()

        for cell in pagecontent.find_all("td"):
            links = cell.find_all("a", href=True)
            if not links:
                continue
            if all("posting.php" in link["href"] for link in links):
                cell.decompose()

        # Print and adjacent-topic controls create duplicate topic variants.
        for cell in pagecontent.find_all("td"):
            links = cell.find_all("a", href=True)
            if links and all(
                (parts := _topic_link_parts(link["href"])) is not None
                and parse_qs(parts.query).get("view", [""])[0]
                in {"print", "previous", "next"}
                for link in links
            ):
                cell.decompose()

        _remove_pagination_step_links(pagecontent)

    _label_post_permalinks(soup)

    # Remove all footer content: dynamic data (online users, stats, login,
    # legend, permissions, search forms) is useless in a static archive.
    for selector in ("#pagefooter", "#wrapfooter"):
        for tag in soup.select(selector):
            tag.decompose()

    # Remove orphaned footer junk that phpBB places outside #pagefooter:
    # RTB ads, "Кто сейчас на конференции", icon legend, permissions,
    # search/jumpbox forms, and accompanying <br> spacers.
    _remove_orphaned_footer_junk(soup)

    # Remove LiveInternet and other tracking/counter scripts
    for script in soup.find_all("script"):
        text = script.get_text()
        if "counter.yadro.ru" in text or "LiveInternet" in text:
            script.decompose()

    # Remove Yandex RTB ad blocks (div containers and their scripts)
    for div in soup.find_all("div", id=lambda x: x and x.startswith("yandex_rtb_")):
        div.decompose()
    for script in soup.find_all("script"):
        if "Ya.Context.AdvManager" in script.get_text() or "yandexContextAsyncCallbacks" in script.get_text():
            script.decompose()

    # Remove HTML comment nodes for ads, phpBB copyright, LiveInternet markers
    _remove_junk_comments(soup)

    # Remove author profile hyperlinks (keep visible text, remove <a> wrapper)
    _unlink_profile_links(soup)

    # Remove reputation change links and icons
    _remove_reputation_elements(soup)


_PAGINATION_STEP_LABELS = {"На страницу", "Пред.", "След."}


def _topic_link_parts(href: str):
    """Recognize both forum URLs and local topic filenames from older archives."""
    parsed = urlparse(urljoin(BASE_URL + "/", href))
    if parsed.netloc not in ("visio.getbb.ru", "www.visio.getbb.ru"):
        return None
    if parsed.path == "/viewtopic.php":
        return parsed
    # Older parsers put the query in the filename and dropped post fragments.
    name = urllib.parse.unquote(parsed.path).rsplit("/", 1)[-1]
    match = re.fullmatch(r"viewtopic(?:__(.*))?\.html", name)
    if match:
        return parsed._replace(path="/viewtopic.php", query=match.group(1) or "")
    return None


def _post_id_from_link(href: str) -> str | None:
    parsed = _topic_link_parts(href)
    if parsed is not None:
        post_id = parse_qs(parsed.query).get("p", [None])[0]
        if post_id and post_id.isdecimal():
            return post_id
    # Same-page anchors are already local; external fragments are not posts here.
    if href.startswith("#") or parsed is not None:
        match = re.fullmatch(r"#p(\d+)", href if href.startswith("#") else "#" + parsed.fragment)
        if match:
            return match.group(1)
    return None


def _remove_pagination_step_links(pagecontent) -> None:
    """Drop the "На страницу", "Пред." and "След." links from page navigation.

    "На страницу" is a JavaScript jump box and the step links duplicate the
    numbered page links, so they are dead or redundant in a static archive.
    """
    for link in pagecontent.find_all("a"):
        if link.get_text(strip=True) not in _PAGINATION_STEP_LABELS:
            continue
        # Drop the spacing that separated the link from its neighbours.
        for sibling in (link.previous_sibling, link.next_sibling):
            if isinstance(sibling, NavigableString) and not sibling.strip():
                sibling.extract()
        link.decompose()


def _label_post_permalinks(soup: BeautifulSoup) -> None:
    """Show the post number ("#p9845") on the permalink icon of each post."""
    for img in soup.find_all("img", src=re.compile(r"icon_post_target")):
        link = img.find_parent("a", href=True)
        if not link:
            continue
        post_id = _post_id_from_link(link["href"])
        if post_id is None:
            continue
        img["alt"] = img["title"] = f"#p{post_id}"
        added = link.find_next_sibling("b")
        if added and added.string and added.string.startswith("Добавлено"):
            added.string = "\u00a0\u00a0" + added.string


def _remove_orphaned_footer_junk(soup: BeautifulSoup) -> None:
    """Remove footer tables/elements that phpBB places outside #pagefooter.

    phpBB sometimes emits the dynamic footer tables (online users, permissions,
    icon legend, search box, jumpbox) as direct children of the outer wrapper
    div (#wrap or body), not inside #pagefooter.  After #pagefooter is removed
    these nodes remain as orphans.  We detect them by their content signatures
    and remove them along with any adjacent <br> spacers.
    """
    FOOTER_TABLE_SIGNATURES = [
        "Кто сейчас на конференции",   # online users table
        "Новые сообщения",              # icon legend table
        "Нет новых сообщений",          # icon legend table
        "Перейти:",                     # jumpbox form
        "Найти:",                       # search form
        "можете начинать темы",       # permissions, logged in or anonymous
        "можете отвечать на сообщения",
        "можете редактировать свои сообщения",
        "можете удалять свои сообщения",
        "можете добавлять вложения",
    ]

    FOOTER_FORM_NAMES = {"search", "jumpbox"}

    def _is_footer_table(tag) -> bool:
        if tag.name != "table":
            return False
        # Footer signatures may also be quoted inside an actual forum post.
        # Never remove a post table, its containing layout, or its nested tables.
        if tag.find(class_="postbody") or tag.find_parent(class_="postbody"):
            return False
        # Check for known footer form names
        for form in tag.find_all("form"):
            if form.get("name") in FOOTER_FORM_NAMES:
                return True
        text = tag.get_text(" ", strip=True)
        return any(sig.casefold() in text.casefold() for sig in FOOTER_TABLE_SIGNATURES)

    # Collect nodes to remove (avoid modifying tree while iterating)
    to_remove = []
    for tag in soup.find_all(True):
        if _is_footer_table(tag):
            # Also remove immediately preceding/following <br> spacers
            prev = tag.previous_sibling
            while prev and getattr(prev, "name", None) in (None, "br") and not getattr(prev, "name", None):
                # NavigableString (whitespace) — step further
                prev = prev.previous_sibling
            if prev and getattr(prev, "name", None) == "br":
                to_remove.append(prev)
            to_remove.append(tag)
    for node in to_remove:
        node.decompose()


def _remove_junk_comments(soup: BeautifulSoup) -> None:
    """Remove HTML comment nodes for ads, phpBB copyright, and tracking markers."""
    from bs4 import Comment

    JUNK_COMMENT_PATTERNS = [
        "Yandex.RTB",
        "LiveInternet",
        "phpbb.com",
        "phpBB Group",
        "We request you retain",
        "Powered by phpBB",
    ]

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        if any(pat in comment for pat in JUNK_COMMENT_PATTERNS):
            comment.extract()


def _unlink_profile_links(soup: BeautifulSoup) -> None:
    """Replace author profile <a> links with plain text, keeping visible content."""
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "viewprofile" in href or (
            "memberlist.php" in href and "mode=viewprofile" in href
        ):
            a.replace_with_children()


def _remove_reputation_elements(soup: BeautifulSoup) -> None:
    """Remove reputation vote links and their associated icons."""
    # Reputation links typically point to posting.php?mode=smilies or
    # a dedicated reputation URL; the most reliable signal is the link text
    # containing '+' / '-' reputation markers, or href containing 'reputation'
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "reputation" in href or "viewprofile" not in href and (
            "post_thanks" in href or "thanks" in href.lower()
        ):
            # Only remove if it looks like a reputation/thanks action link
            if "reputation" in href or "post_thanks" in href:
                a.decompose()
    # Remove reputation score spans/divs (class names vary by phpBB style)
    for tag in soup.find_all(class_=lambda c: c and any(
        "reputa" in x.lower() or "thanks" in x.lower() for x in (c if isinstance(c, list) else [c])
    )):
        tag.decompose()


def _declared_topic_post_count(soup: BeautifulSoup) -> int | None:
    """Return the post count shown by phpBB topic navigation, if present."""
    pagecontent = soup.find(id="pagecontent")
    if not pagecontent:
        return None

    text = pagecontent.get_text(" ", strip=True)
    match = re.search(r"\[\s*Сообщ(?:ений|ение|ения):\s*(\d+)\s*\]", text)
    if match:
        return int(match.group(1))
    return None


def _has_topic_posts(soup: BeautifulSoup) -> bool:
    pagecontent = soup.find(id="pagecontent")
    if not pagecontent:
        return False
    return bool(
        pagecontent.select_one(
            ".postbody, .postdetails, .postprofile, .postauthor, .postsubject"
        )
    )


def _is_incomplete_topic_page(url: str, soup: BeautifulSoup) -> bool:
    parsed = urlparse(url)
    if not parsed.path.endswith("/viewtopic.php") and parsed.path != "/viewtopic.php":
        return False
    declared_count = _declared_topic_post_count(soup)
    return declared_count is not None and declared_count > 0 and not _has_topic_posts(soup)

# URL patterns to skip entirely
SKIP_PATTERNS = [
    "viewprofile",
    "search.php",
    "ucp.php",
    "memberlist.php",
    "login",
    "logout",
    "register",
    "posting.php",
    "report.php",
    "mcp.php",
    "adm/",
]

# Parameters to strip from saved URLs (session-specific, not needed for static mirror)
STRIP_PARAMS = {"sid", "st", "sk", "sd"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    """Return a canonical URL with session params stripped."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.path.endswith("/viewtopic.php") or parsed.path == "/viewtopic.php":
        # Post permalinks and print variants are alternate ways to reach a
        # topic, not separate pages worth archiving. ``view=next/previous`` is
        # removed from page chrome instead: the server resolves it to another
        # topic, so rewriting it to the current ``t`` would be incorrect.
        params.pop("p", None)
        if params.get("view") == ["print"]:
            params.pop("view", None)
    cleaned = {k: v for k, v in params.items() if k not in STRIP_PARAMS}
    new_query = urlencode({k: v[0] for k, v in cleaned.items()}, doseq=False)
    return urlunparse(parsed._replace(query=new_query, fragment=""))


def should_skip(url: str) -> bool:
    """Return True if this URL should not be crawled."""
    for pattern in SKIP_PATTERNS:
        if pattern in url:
            return True
    return False


def url_to_local_path(url: str, output_dir: Path) -> Path:
    """Map a forum URL to a local file path."""
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")
    query = parsed.query

    if not path:
        path = "index.html"
    elif path.endswith("/"):
        path = path + "index.html"
    else:
        base, ext = os.path.splitext(path)
        if not ext:
            path = path + ".html"
        elif ext.lower() == ".php":
            # Convert .php pages to .html for clean local archive
            path = base + ".html"

    if query:
        # Encode query string into filename safely
        safe_query = re.sub(r"[^a-zA-Z0-9_=&.-]", "_", query)
        base, ext = os.path.splitext(path)
        path = f"{base}__{safe_query}{ext}"

    return output_dir / path


def is_forum_page(url: str) -> bool:
    """Return True if URL is a forum HTML page (not a file download)."""
    parsed = urlparse(url)
    path = parsed.path
    known_pages = {"/viewforum.php", "/viewtopic.php", "/index.php", "/"}
    for p in known_pages:
        if path == p or path.endswith(p):
            return True
    # download/file.php is a binary download
    if "download/file.php" in path:
        return False
    if path.endswith(".php"):
        return True
    return False


def detect_extension_from_response(response: requests.Response, url: str) -> str:
    """Guess file extension from Content-Disposition, Content-Type, or URL."""
    # 1. Content-Disposition: attachment; filename="file.7z"
    cd = response.headers.get("Content-Disposition", "")
    if cd:
        match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, re.IGNORECASE)
        if match:
            fname = match.group(1).strip().strip('"\'')
            _, cd_ext = os.path.splitext(fname)
            if cd_ext:
                return cd_ext.lower()

    content_type = response.headers.get("Content-Type", "")
    mime = content_type.split(";")[0].strip()
    mime_to_ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z",
        "application/vnd.ms-visio.drawing": ".vsd",
        "application/vnd.visio": ".vsd",
        "application/octet-stream": "",
    }
    ext = mime_to_ext.get(mime, mimetypes.guess_extension(mime) or "")

    if not ext:
        # Fall back to URL path extension
        url_path = urlparse(url).path
        _, url_ext = os.path.splitext(url_path)
        if url_ext:
            ext = url_ext
    return ext


def _extract_index_sections(soup: BeautifulSoup) -> list[dict]:
    """Parse forum index page: return list of top-level sections with subsections."""
    sections = []
    current_section: dict | None = None

    pagecontent = soup.find(id="pagecontent")
    if not pagecontent:
        return sections

    for row in pagecontent.find_all("tr"):
        # Section header row (cat class)
        cat = row.find("td", class_="cat")
        if cat and not row.find("td", class_=re.compile(r"^(row|forumrow)")):
            title = cat.get_text(" ", strip=True)
            current_section = {"title": title, "subsections": []}
            sections.append(current_section)
            continue

        if current_section is None:
            continue

        # Subsection row
        forum_link = row.find("a", href=re.compile(r"viewforum\.php"))
        if forum_link:
            href = forum_link.get("href", "")
            title = forum_link.get_text(" ", strip=True)
            desc_td = row.find("td", class_=re.compile(r"(row2|forumrow)"))
            desc = ""
            if desc_td:
                # Description is usually in a <span class="genmed"> or the td's own text
                desc_tag = desc_td.find("span", class_="genmed") or desc_td.find("p")
                if desc_tag:
                    desc = desc_tag.get_text(" ", strip=True)
            current_section["subsections"].append({
                "title": title,
                "url": href,
                "description": desc,
                "threads": [],
            })

    return sections


def _extract_forum_threads(soup: BeautifulSoup) -> list[dict]:
    """Parse a viewforum page: return list of thread stubs."""
    threads = []
    pagecontent = soup.find(id="pagecontent")
    if not pagecontent:
        return threads

    for row in pagecontent.find_all("tr"):
        link = row.find("a", href=re.compile(r"viewtopic\.php"))
        if not link:
            continue
        title = link.get_text(" ", strip=True)
        href = link.get("href", "")
        threads.append({"title": title, "url": href, "posts": []})

    return threads


def _extract_topic_posts(soup: BeautifulSoup) -> list[dict]:
    """Parse a viewtopic page: return list of post dicts."""
    posts = []
    pagecontent = soup.find(id="pagecontent")
    if not pagecontent:
        return posts

    for postbody in pagecontent.find_all(class_="postbody"):
        post: dict = {}

        # Author: look in the same row's postprofile cell
        row = postbody.find_parent("tr")
        if row:
            profile = row.find(class_="postprofile") or row.find(class_="postauthor")
            if profile:
                post["author"] = profile.get_text(" ", strip=True)

        # Post subject
        subject = postbody.find(class_="postsubject") or postbody.find(class_="subject")
        if subject:
            post["subject"] = subject.get_text(" ", strip=True)

        # Post text (all text inside postbody, excluding subject)
        if subject:
            subject.extract()
        post["text"] = postbody.get_text(" ", strip=True)

        # Attachments / file links within this post
        attachments = []
        for a in postbody.find_all("a", href=True):
            href = a.get("href", "")
            if "download/" in href or "file.php" in href:
                attachments.append({"label": a.get_text(strip=True), "url": href})
        if attachments:
            post["attachments"] = attachments

        posts.append(post)

    return posts


POST_LINK_ATTR = "data-archive-post"
_POST_ANCHOR_RE = re.compile(r"^p(\d+)$")


def _page_post_ids(soup: BeautifulSoup) -> set[str]:
    """Return ids of posts anchored on this page (``id``/``name`` = ``p<id>``)."""
    ids: set[str] = set()
    for tag in soup.find_all(attrs={"id": _POST_ANCHOR_RE}):
        ids.add(_POST_ANCHOR_RE.match(tag["id"]).group(1))
    for tag in soup.find_all(attrs={"name": _POST_ANCHOR_RE}):
        ids.add(_POST_ANCHOR_RE.match(tag["name"]).group(1))
    return ids


def _local_path_to_url(path: Path, output_dir: Path) -> str:
    """Invert ``url_to_local_path`` for saved ``viewtopic`` pages."""
    rel = path.relative_to(output_dir).as_posix()
    base, _ext = os.path.splitext(rel)
    name, _sep, query = base.partition("__")
    url = f"{BASE_URL}/{name}.php"
    return f"{url}?{query}" if query else url


def build_post_index(output_dir: Path) -> dict[str, str]:
    """Map every archived post id to the topic page URL that contains it."""
    index: dict[str, str] = {}
    for path in sorted(output_dir.glob("viewtopic*.html")):
        with open(path, encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        page_url = _local_path_to_url(path, output_dir)
        for post_id in _page_post_ids(soup):
            index.setdefault(post_id, page_url)
    return dict(sorted(index.items(), key=lambda item: int(item[0])))


def _build_forum_structure(output_dir: Path) -> dict:
    """Walk saved HTML files and assemble the nested forum dictionary."""
    result: dict = {"sections": []}

    index_path = output_dir / "index.html"
    if not index_path.exists():
        log.warning("forum.json: index.html not found in %s, skipping section structure", output_dir)
        return result

    with open(index_path, encoding="utf-8") as f:
        index_soup = BeautifulSoup(f.read(), "html.parser")

    sections = _extract_index_sections(index_soup)
    result["sections"] = sections

    # For each subsection, find saved viewforum pages and populate threads
    for section in sections:
        for subsection in section.get("subsections", []):
            forum_url = subsection["url"]
            # Derive local path from URL (may have query string like f=5)
            norm = normalize_url(urljoin(BASE_URL + "/", forum_url.lstrip("./")))
            forum_path = url_to_local_path(norm, output_dir)
            if not forum_path.exists():
                continue
            with open(forum_path, encoding="utf-8") as f:
                forum_soup = BeautifulSoup(f.read(), "html.parser")
            threads = _extract_forum_threads(forum_soup)

            for thread in threads:
                topic_url = thread["url"]
                t_norm = normalize_url(urljoin(BASE_URL + "/", topic_url.lstrip("./")))
                topic_path = url_to_local_path(t_norm, output_dir)
                if topic_path.exists():
                    with open(topic_path, encoding="utf-8") as f:
                        topic_soup = BeautifulSoup(f.read(), "html.parser")
                    thread["posts"] = _extract_topic_posts(topic_soup)

            subsection["threads"] = threads

    return result


def is_transient_error(exc: requests.RequestException) -> bool:
    """Return True if retrying `exc` later could plausibly succeed.

    Timeouts and connection errors are the throttle/tarpit signature seen in
    issue #17: the attachment exists and downloads fine once the server stops
    stalling. HTTP 4xx (other than 429) is a permanent answer, so retrying it
    only wastes time on links that are simply dead.
    """
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status == 429 or status >= 500
    return True


class ForumParser:
    def __init__(
        self,
        output_dir: str,
        delay: float = 0.1,
        max_pages: int = 0,
        resume: bool = False,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
        retry_passes: int = RETRY_PASSES,
    ):
        self.output_dir = Path(output_dir)
        self.delay = delay
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.retry_passes = retry_passes
        self.max_pages = max_pages  # 0 = unlimited
        self.resume = resume
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; ForumArchiver/1.0; "
                    "+https://github.com/Surrogate-TM/save_visiogetbb)"
                )
            }
        )
        self.visited_pages: set[str] = set()
        self.failed_pages: dict[str, bool] = {}  # URL -> temporary failure
        self.page_attempts: dict[str, int] = {}
        self.downloaded_files: dict[str, Path] = {}  # url -> local path
        # Remembering failures keeps a dead or stalled URL from being re-fetched
        # once per referencing page (one attachment cost 42 attempts in #17).
        self.failed_downloads: dict[str, str] = {}  # url -> reason
        self.retry_queue: dict[str, str] = {}  # url -> "file" | "image"
        self.download_attempts: dict[str, int] = {}  # url -> attempts spent
        # Forum posts that reference each attachment, for the failure report.
        self.attachment_posts: dict[str, list[str]] = {}
        self.queue: deque[str] = deque()
        self.pages_saved = 0
        self._download_log_handler: logging.FileHandler | None = None
        self._download_log: logging.Logger | None = None
        self._last_request_at: float | None = None
        self._consecutive_failures = 0
        self._host_failures: dict[str, int] = {}
        self.last_failure_transient = False
        self.last_attempts_used = 0

    def _init_download_log(self) -> None:
        """Set up a dedicated file logger for download results (called after output_dir is created)."""
        if self._download_log is not None:
            return
        log_path = self.output_dir / "downloads.log"
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        # Use a unique logger name per output directory to avoid handler accumulation
        # across instances (id() can be reused after GC, so use the path instead)
        dl_log = logging.Logger(f"forum_downloads.{self.output_dir}")
        dl_log.setLevel(logging.DEBUG)
        dl_log.addHandler(handler)
        self._download_log_handler = handler
        self._download_log = dl_log

    def _log_download_ok(self, url: str, local_path: Path) -> None:
        if self._download_log:
            self._download_log.info("OK %s -> %s", url, local_path)

    def _log_download_fail(self, url: str, reason: str) -> None:
        if self._download_log:
            self._download_log.error("FAIL %s : %s", url, reason)

    def _record_download_failure(self, norm: str, url: str, kind: str) -> None:
        """Remember a failed download and queue it for a deferred retry."""
        transient = self.last_failure_transient
        reason = "fetch failed (temporary)" if transient else "fetch failed (permanent)"
        self.failed_downloads[norm] = reason
        if transient and self._attempts_left(norm) > 0:
            self.retry_queue[norm] = kind
        self._log_download_fail(url, reason)

    def _attempts_left(self, norm: str) -> int:
        return ATTACHMENT_ATTEMPTS - self.download_attempts.get(norm, 0)

    def _attempts_now(self, norm: str) -> int:
        """Attempts to spend on this fetch, keeping some for deferred passes."""
        if norm in self.download_attempts:
            return 1
        reserved = self.retry_passes
        return max(1, min(self._attempts_left(norm), ATTACHMENT_ATTEMPTS - reserved))

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def fetch(self, url: str, attempts: int = 1) -> requests.Response | None:
        """Fetch a URL, retrying only failures that can plausibly succeed later.

        Sets ``self.last_failure_transient`` so callers can tell a temporary
        stall (worth a deferred retry) from a permanent error such as 404.
        """
        self.last_failure_transient = False
        self.last_attempts_used = 0
        host = urlparse(url).netloc.lower()
        if self._host_failures.get(host, 0) >= HOST_FAILURE_LIMIT:
            self.last_failure_transient = True
            log.debug("Deferring %s: host is temporarily unavailable", url)
            return None

        for attempt in range(1, attempts + 1):
            try:
                if self.delay > 0 and self._last_request_at is not None:
                    elapsed = time.monotonic() - self._last_request_at
                    if elapsed < self.delay:
                        time.sleep(self.delay - elapsed)
                resp = self.session.get(
                    url,
                    timeout=(self.connect_timeout, self.read_timeout),
                    allow_redirects=True,
                )
                self._last_request_at = time.monotonic()
                resp.raise_for_status()
                self._host_failures[host] = 0
                self._consecutive_failures = 0
                self.last_attempts_used = attempt
                return resp
            except requests.RequestException as e:
                self._last_request_at = time.monotonic()
                self.last_attempts_used = attempt
                transient = is_transient_error(e)
                self.last_failure_transient = transient

                if not transient:
                    # 404/403 and friends will never succeed; retrying only
                    # multiplies the cost of a link that is simply dead.
                    log.warning("Failed to fetch %s (permanent): %s", url, e)
                    return None

                self._host_failures[host] = self._host_failures.get(host, 0) + 1
                self._consecutive_failures = self._host_failures[host]
                self._cool_down_if_throttled()
                if self._consecutive_failures >= HOST_FAILURE_LIMIT:
                    log.warning(
                        "Host %s failed %d consecutive requests; deferring further "
                        "requests until the next retry pass (use --resume later). "
                        "Last failure: %s: %s",
                        host, self._consecutive_failures, url, e,
                    )
                    return None

                if attempt == attempts:
                    log.warning(
                        "Failed to fetch %s after %d attempt(s): %s",
                        url,
                        attempts,
                        e,
                    )
                    return None

                retry_delay = ATTACHMENT_RETRY_DELAY * attempt
                log.warning(
                    "Failed to fetch %s (attempt %d/%d): %s; retrying in %.1fs",
                    url,
                    attempt,
                    attempts,
                    e,
                    retry_delay,
                )
                time.sleep(retry_delay)

        return None

    def _cool_down_if_throttled(self) -> None:
        """Pause after a run of transient failures so a rate limit can expire."""
        if self._consecutive_failures < THROTTLE_TRIGGER:
            return
        overshoot = min(self._consecutive_failures - THROTTLE_TRIGGER, 3)
        pause = min(THROTTLE_BACKOFF_BASE * (2**overshoot), THROTTLE_BACKOFF_MAX)
        log.warning(
            "%d consecutive request failures; backing off for %.1fs "
            "before probing the server again",
            self._consecutive_failures,
            pause,
        )
        # Drop pooled sockets: a tarpitted keep-alive connection stays stalled.
        self.session.close()
        time.sleep(pause)

    # ------------------------------------------------------------------
    # File download helpers
    # ------------------------------------------------------------------

    def download_file(self, url: str) -> Path | None:
        """Download a binary file and return its local path."""
        norm = normalize_url(url)
        if norm in self.downloaded_files:
            return self.downloaded_files[norm]
        if norm in self.failed_downloads:
            # Already attempted this run; a deferred pass will retry it if the
            # failure was transient. Re-fetching now costs a stall per page.
            return None

        if self._attempts_left(norm) <= 0:
            return None
        resp = self.fetch(url, attempts=self._attempts_now(norm))
        self.download_attempts[norm] = (
            self.download_attempts.get(norm, 0) + self.last_attempts_used
        )
        if resp is None:
            self._record_download_failure(norm, url, "file")
            return None

        ext = detect_extension_from_response(resp, url)

        # Build a stable local name from the URL
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        file_id = query_params.get("id", [""])[0]

        if "download/file.php" in parsed.path and file_id:
            local_name = f"file_{file_id}{ext}"
            local_path = self.output_dir / "download" / local_name
        else:
            # For images and other assets, keep path structure
            rel_path = parsed.path.lstrip("/")
            if not rel_path:
                rel_path = "unknown"
            _, url_ext = os.path.splitext(rel_path)
            if not url_ext and ext:
                rel_path = rel_path + ext
            local_path = self.output_dir / rel_path

        local_path.parent.mkdir(parents=True, exist_ok=True)

        if not local_path.exists():
            with open(local_path, "wb") as f:
                f.write(resp.content)
            log.debug("Downloaded file: %s -> %s", url, local_path)

        self.downloaded_files[norm] = local_path
        self._log_download_ok(url, local_path)
        return local_path

    def download_image(self, url: str) -> Path | None:
        """Download an image (possibly external) and return local path."""
        norm = normalize_url(url)
        if norm in self.downloaded_files:
            return self.downloaded_files[norm]
        if norm in self.failed_downloads:
            return None

        parsed = urlparse(url)
        # Build a safe local path under images/
        # Use host + path to avoid collisions with external images
        host = parsed.netloc.replace(".", "_")
        rel = parsed.path.lstrip("/")
        if not rel:
            rel = "image"
        _, ext = os.path.splitext(rel)

        if self._attempts_left(norm) <= 0:
            return None
        resp = self.fetch(url, attempts=self._attempts_now(norm))
        self.download_attempts[norm] = (
            self.download_attempts.get(norm, 0) + self.last_attempts_used
        )
        if resp is None:
            self._record_download_failure(norm, url, "image")
            return None

        if not ext:
            ext = detect_extension_from_response(resp, url)
            rel = rel + ext

        local_path = self.output_dir / "images_cache" / host / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if not local_path.exists():
            with open(local_path, "wb") as f:
                f.write(resp.content)
            log.debug("Downloaded image: %s -> %s", url, local_path)

        self.downloaded_files[norm] = local_path
        self._log_download_ok(url, local_path)
        return local_path

    # ------------------------------------------------------------------
    # Link rewriting
    # ------------------------------------------------------------------

    def make_relative(self, from_path: Path, to_path: Path) -> str:
        """Return a relative URL from from_path to to_path."""
        try:
            rel = os.path.relpath(to_path, from_path.parent)
            return rel.replace(os.sep, "/")
        except ValueError:
            # On Windows, different drives – fall back to absolute
            return "/" + str(to_path.relative_to(self.output_dir)).replace(os.sep, "/")

    def rewrite_url(self, url: str, current_page_path: Path) -> str:
        """Rewrite a forum URL to a local relative path."""
        if not url or url.startswith("#"):
            return url

        # Resolve relative URLs against base
        if not url.startswith("http"):
            url = urljoin(BASE_URL + "/", url.lstrip("./"))

        parsed = urlparse(url)

        # External non-visio URLs: keep as-is (images may be downloaded separately)
        if parsed.netloc and parsed.netloc not in ("visio.getbb.ru", "www.visio.getbb.ru"):
            return url

        norm = normalize_url(url)

        if should_skip(norm):
            return url  # keep original, just don't crawl

        local_path = url_to_local_path(norm, self.output_dir)
        return self.make_relative(current_page_path, local_path)

    # ------------------------------------------------------------------
    # HTML processing
    # ------------------------------------------------------------------

    def _note_attachment_post(self, attachment_url: str, tag, page_url: str) -> None:
        """Remember which forum post links to an attachment."""
        anchor = tag.find_previous(attrs={"id": _POST_ANCHOR_RE}) or tag.find_previous(
            attrs={"name": _POST_ANCHOR_RE}
        )
        if anchor is not None:
            post_id = _POST_ANCHOR_RE.match(anchor.get("id") or anchor["name"]).group(1)
            post_url = f"{BASE_URL}/viewtopic.php?p={post_id}#p{post_id}"
        else:
            post_url = page_url
        posts = self.attachment_posts.setdefault(normalize_url(attachment_url), [])
        if post_url not in posts:
            posts.append(post_url)

    def write_failed_attachments(self) -> int:
        """Write a Markdown table of attachments that never downloaded."""
        rows = [
            (post, url)
            for url, posts in self.attachment_posts.items()
            if url in self.failed_downloads
            for post in posts
        ]
        lines = ["| Сообщение форума | Путь к вложению |", "| --- | --- |"]
        lines += [f"| {post} | {url} |" for post, url in rows]
        path = self.output_dir / FAILED_ATTACHMENTS_FILE
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log.info("Failed attachments: %d (listed in %s)", len(rows), path)
        return len(rows)

    def process_page(self, url: str, html: str) -> str:
        """Process HTML: rewrite links, download assets, return modified HTML."""
        norm_page = normalize_url(url)
        local_path = url_to_local_path(norm_page, self.output_dir)
        html = _fix_self_closing_spans(html)
        soup = BeautifulSoup(html, "html.parser")
        _remove_forum_chrome(soup)
        page_post_ids = _page_post_ids(soup)

        # --- Rewrite <a href> links ---
        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            if not href or href.startswith("mailto:") or href.startswith("javascript:"):
                continue

            abs_href = urljoin(url, href)
            parsed = urlparse(abs_href)

            # Only process links on the same domain
            if parsed.netloc and parsed.netloc not in ("visio.getbb.ru", "www.visio.getbb.ru"):
                continue

            norm = normalize_url(abs_href)

            if should_skip(norm):
                tag["href"] = "#"
                continue

            # phpBB post permalinks: posts on this page become local anchors.
            # Posts elsewhere (e.g. quotes from other topics) are resolved
            # after the crawl, once we know which page holds each post.
            post_ids = parse_qs(parsed.query).get("p", [])
            if (
                post_ids
                and (parsed.path.endswith("/viewtopic.php") or parsed.path == "/viewtopic.php")
            ):
                post_id = post_ids[0]
                if post_id in page_post_ids:
                    tag["href"] = f"#p{post_id}"
                else:
                    tag["href"] = f"{BASE_URL}/viewtopic.php?p={post_id}#p{post_id}"
                    tag[POST_LINK_ATTR] = post_id
                continue

            # File downloads: download and rewrite
            if "download/file.php" in parsed.path:
                self._note_attachment_post(abs_href, tag, norm_page)
                local_file = self.download_file(abs_href)
                if local_file:
                    tag["href"] = self.make_relative(local_path, local_file)
                continue

            # Forum pages: rewrite to local path and enqueue
            if is_forum_page(norm):
                if norm not in self.visited_pages:
                    self.queue.append(norm)
                tag["href"] = self.rewrite_url(abs_href, local_path)
            else:
                tag["href"] = self.rewrite_url(abs_href, local_path)

        # --- Rewrite <img src> and download images ---
        for tag in soup.find_all("img", src=True):
            src = tag["src"]
            abs_src = urljoin(url, src)

            # Skip tiny icons/smilies that are part of the forum skin
            parsed_src = urlparse(abs_src)
            path_lower = parsed_src.path.lower()
            if any(
                x in path_lower
                for x in ["/styles/", "/images/smilies/", "/images/icons/"]
            ):
                # Still rewrite to local path if it's on the same domain
                if parsed_src.netloc in ("", "visio.getbb.ru", "www.visio.getbb.ru"):
                    tag["src"] = self.rewrite_url(abs_src, local_path)
                continue

            if "download/file.php" in parsed_src.path:
                self._note_attachment_post(abs_src, tag, norm_page)
                local_file = self.download_file(abs_src)
                if local_file:
                    tag["src"] = self.make_relative(local_path, local_file)
                continue

            # Download user-content images (may be external)
            local_img = self.download_image(abs_src)
            if local_img:
                tag["src"] = self.make_relative(local_path, local_img)

        # --- Download attachments embedded through the Office viewer ---
        for tag in soup.find_all("iframe", src=True):
            abs_src = urljoin(url, tag["src"])
            parsed_src = urlparse(abs_src)
            attachment_url = None

            if "download/file.php" in parsed_src.path:
                attachment_url = abs_src
            elif parsed_src.netloc == "view.officeapps.live.com":
                values = parse_qs(parsed_src.query).get("src", [])
                if values:
                    candidate = values[0]
                    candidate_parsed = urlparse(candidate)
                    if (
                        candidate_parsed.netloc
                        in ("visio.getbb.ru", "www.visio.getbb.ru")
                        and "download/file.php" in candidate_parsed.path
                    ):
                        attachment_url = candidate

            if attachment_url:
                self._note_attachment_post(attachment_url, tag, norm_page)
                local_file = self.download_file(attachment_url)
                if local_file:
                    tag["src"] = self.make_relative(local_path, local_file)

        # --- Rewrite <link href> (CSS) ---
        for tag in soup.find_all("link", href=True):
            abs_href = urljoin(url, tag["href"])
            parsed = urlparse(abs_href)
            if parsed.netloc in ("", "visio.getbb.ru", "www.visio.getbb.ru"):
                local_asset = self.download_image(abs_href)
                if local_asset:
                    tag["href"] = self.make_relative(local_path, local_asset)

        # --- Rewrite <script src> ---
        for tag in soup.find_all("script", src=True):
            abs_src = urljoin(url, tag["src"])
            parsed = urlparse(abs_src)
            if parsed.netloc in ("", "visio.getbb.ru", "www.visio.getbb.ru"):
                local_asset = self.download_image(abs_src)
                if local_asset:
                    tag["src"] = self.make_relative(local_path, local_asset)

        # --- Remove session IDs from all remaining internal links ---
        # (catch any link types we might have missed)
        for tag in soup.find_all(href=re.compile(r"sid=")):
            tag["href"] = re.sub(r"[?&]sid=[a-f0-9]+", "", tag["href"])
        for tag in soup.find_all(src=re.compile(r"sid=")):
            tag["src"] = re.sub(r"[?&]sid=[a-f0-9]+", "", tag["src"])

        return soup.decode(formatter=_SpoilerSafeFormatter())

    # ------------------------------------------------------------------
    # Page saving
    # ------------------------------------------------------------------

    def save_page(self, url: str) -> None:
        norm = normalize_url(url)
        if norm in self.visited_pages:
            return
        self.visited_pages.add(norm)

        if self.max_pages and self.pages_saved >= self.max_pages:
            return

        local_path = url_to_local_path(norm, self.output_dir)
        if self.resume and local_path.exists():
            log.info("Resume: skipping already saved %s", norm)
            self._repair_saved_chrome(local_path)
            self._enqueue_saved_links(local_path)
            self.pages_saved += 1
            return

        log.info("[%d] Fetching: %s", self.pages_saved + 1, norm)
        resp = self.fetch(norm)
        self.page_attempts[norm] = self.page_attempts.get(norm, 0) + self.last_attempts_used
        if resp is None:
            self.failed_pages[norm] = self.last_failure_transient
            return

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            log.debug("Skipping non-HTML content at %s", norm)
            return

        soup = BeautifulSoup(_fix_self_closing_spans(resp.text), "html.parser")
        if _is_incomplete_topic_page(norm, soup):
            declared_count = _declared_topic_post_count(soup)
            log.warning(
                "Skipping incomplete topic page %s: navigation declares %s posts, "
                "but no post markup was found",
                norm,
                declared_count,
            )
            self.failed_pages[norm] = True
            return

        processed_html = self.process_page(norm, resp.text)

        local_path = url_to_local_path(norm, self.output_dir)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        with open(local_path, "w", encoding="utf-8") as f:
            f.write(processed_html)

        self.pages_saved += 1
        self.failed_pages.pop(norm, None)
        log.info("Saved: %s -> %s", norm, local_path.relative_to(self.output_dir))

    def _enqueue_saved_links(self, path: Path) -> None:
        """Continue discovery through cached pages, whose links are already local."""
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            if not href or href.startswith("#"):
                continue
            parsed = urlparse(href)
            if parsed.scheme or parsed.netloc:
                if parsed.netloc not in ("visio.getbb.ru", "www.visio.getbb.ru"):
                    continue
                url = href
            else:
                # '?' and '&' are part of the archive filename, not a URL query.
                local = path.parent / urllib.parse.unquote(href.split("#", 1)[0])
                try:
                    rel = local.resolve().relative_to(self.output_dir.resolve())
                except ValueError:
                    continue
                if rel.suffix != ".html":
                    continue
                name, _, query = rel.with_suffix("").as_posix().partition("__")
                url = f"{BASE_URL}/{name}.php"
                if query:
                    url += f"?{query}"
            parsed = urlparse(url)
            if parse_qs(parsed.query).get("p"):
                continue
            norm = normalize_url(url)
            if is_forum_page(norm) and not should_skip(norm) and norm not in self.visited_pages:
                self.queue.append(norm)

    # ------------------------------------------------------------------
    # Crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, start_url: str = BASE_URL) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._init_download_log()
        settings = (
            f"ForumArchiver {PARSER_VERSION}; script={Path(__file__).resolve()}; "
            f"connect_timeout={self.connect_timeout:g}s; read_timeout={self.read_timeout:g}s; "
            f"retry_passes={self.retry_passes}; attachment_attempts={ATTACHMENT_ATTEMPTS}; "
            f"delay={self.delay:g}s; resume={self.resume}"
        )
        log.info(settings)
        self._download_log.info(settings)
        start_norm = normalize_url(start_url)
        self.queue.append(start_norm)

        try:
            self._drain_page_queue()
            for pass_no in range(1, self.retry_passes + 1):
                pending = [url for url, temporary in self.failed_pages.items()
                           if temporary and self.page_attempts.get(url, 0) < ATTACHMENT_ATTEMPTS]
                if not pending or (self.max_pages and self.pages_saved >= self.max_pages):
                    break
                log.info("Page retry pass %d/%d: %d page(s)", pass_no, self.retry_passes, len(pending))
                self.session.close()
                self._host_failures.clear()
                self._consecutive_failures = 0
                if RETRY_PASS_PAUSE > 0:
                    time.sleep(RETRY_PASS_PAUSE)
                for url in pending:
                    self.visited_pages.discard(url)
                    self.queue.append(url)
                self._drain_page_queue()
            self.retry_failed_downloads()
        finally:
            # Ctrl-C must still leave a usable archive and a post index.
            self.resolve_download_links()
            self.resolve_post_links()
            self.write_failed_attachments()
            with open(self.output_dir / "failed_pages.json", "w", encoding="utf-8") as f:
                json.dump(sorted(self.failed_pages), f, ensure_ascii=False, indent=2)
            log.info("Pages still failing: %d (failed_pages.json)", len(self.failed_pages))
            self._export_json()

        log.info(
            "Crawl complete. Pages saved: %d, Files downloaded: %d, "
            "Downloads still failing: %d",
            self.pages_saved,
            len(self.downloaded_files),
            len(self.failed_downloads),
        )

    def _drain_page_queue(self) -> None:
        while self.queue:
            if self.max_pages and self.pages_saved >= self.max_pages:
                log.info("Reached max_pages limit (%d), stopping.", self.max_pages)
                break

            url = self.queue.popleft()
            norm = normalize_url(url)

            if norm in self.visited_pages:
                continue
            if should_skip(norm):
                continue

            self.save_page(norm)

    def retry_failed_downloads(self) -> int:
        """Re-attempt transiently-failed downloads after the crawl.

        By this point the throttle that stalled them has usually expired, so a
        deferred pass recovers attachments that no amount of in-place retrying
        could get. Returns the number of downloads recovered.
        """
        recovered = 0
        for pass_no in range(1, self.retry_passes + 1):
            if not self.retry_queue:
                break

            pending = dict(self.retry_queue)
            self.retry_queue.clear()
            log.info(
                "Retry pass %d/%d for %d failed download(s)",
                pass_no,
                self.retry_passes,
                len(pending),
            )
            # Start from fresh sockets and give the server a moment to recover.
            self.session.close()
            self._host_failures.clear()
            self._consecutive_failures = 0
            if RETRY_PASS_PAUSE > 0:
                time.sleep(RETRY_PASS_PAUSE)

            for norm, kind in pending.items():
                # Clear the memoized failure so the download is attempted again.
                self.failed_downloads.pop(norm, None)
                if kind == "image":
                    result = self.download_image(norm)
                else:
                    result = self.download_file(norm)
                if result is not None:
                    recovered += 1

        if recovered:
            log.info("Recovered %d download(s) in deferred retry passes", recovered)
        return recovered

    def resolve_download_links(self) -> int:
        """Update saved HTML references to assets recovered in deferred passes."""
        rewritten = 0
        for path in sorted(self.output_dir.glob("*.html")):
            soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
            changed = False
            for tag in soup.find_all(["a", "img", "iframe", "link", "script"]):
                attr = "href" if tag.name in {"a", "link"} else "src"
                value = tag.get(attr)
                if not value or value.startswith("#"):
                    continue
                url = urljoin(BASE_URL + "/", value)
                parsed = urlparse(url)
                if tag.name == "iframe" and parsed.netloc == "view.officeapps.live.com":
                    url = parse_qs(parsed.query).get("src", [url])[0]
                target = self.downloaded_files.get(normalize_url(url))
                if target is not None:
                    tag[attr] = self.make_relative(path, target)
                    changed = True
                    rewritten += 1
            if changed:
                path.write_text(soup.decode(formatter=_SpoilerSafeFormatter()), encoding="utf-8")
        log.info("Resolved %d recovered asset link(s)", rewritten)
        return rewritten

    def resolve_post_links(self) -> int:
        """Point cross-page post links at the saved page holding the post.

        Writes ``posts.json`` (post id -> topic page URL) and rewrites both
        marked links and legacy local filenames. Posts that were not archived
        keep their online URL. Returns the number of links rewritten.
        """
        index = build_post_index(self.output_dir)
        with open(self.output_dir / "posts.json", "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        log.info("Post index written: %d post(s)", len(index))

        rewritten = 0
        for path in sorted(self.output_dir.glob("*.html")):
            with open(path, encoding="utf-8") as f:
                html = f.read()
            if POST_LINK_ATTR not in html and "viewtopic" not in html and "#p" not in html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            page_post_ids = _page_post_ids(soup)
            changed = False
            for tag in soup.find_all("a", href=True):
                post_id = tag.get(POST_LINK_ATTR)
                if post_id is None:
                    post_id = _post_id_from_link(tag["href"])
                if post_id is None:
                    continue
                page_url = index.get(post_id)
                if page_url is None:
                    log.debug("Post %s has no saved target yet (%s)", post_id, path.name)
                    # A legacy local filename without a target would be a dead link.
                    # Keep the original forum available until the post is archived.
                    fallback = f"{BASE_URL}/viewtopic.php?p={post_id}#p{post_id}"
                    if tag["href"] != fallback:
                        tag["href"] = fallback
                        changed = True
                        rewritten += 1
                    continue
                target = url_to_local_path(page_url, self.output_dir)
                if post_id in page_post_ids:
                    destination = f"#p{post_id}"
                else:
                    destination = f"{self.make_relative(path, target)}#p{post_id}"
                if tag["href"] == destination and POST_LINK_ATTR not in tag.attrs:
                    continue
                tag["href"] = destination
                tag.attrs.pop(POST_LINK_ATTR, None)
                changed = True
                rewritten += 1
            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(soup.decode(formatter=_SpoilerSafeFormatter()))
        log.info("Resolved %d cross-page post link(s)", rewritten)
        return rewritten

    def _repair_saved_chrome(self, path: Path) -> bool:
        """Clean an existing page without fetching or rewriting local assets."""
        html = path.read_text(encoding="utf-8")
        soup = BeautifulSoup(_fix_self_closing_spans(html), "html.parser")
        _remove_forum_chrome(soup)
        repaired = soup.decode(formatter=_SpoilerSafeFormatter())
        if repaired == html:
            return False
        path.write_text(repaired, encoding="utf-8")
        log.debug("Repaired saved page chrome: %s", path)
        return True

    def repair_archive(self) -> int:
        """Repair old headers, footers and post links entirely offline."""
        repaired = sum(self._repair_saved_chrome(path)
                       for path in sorted(self.output_dir.glob("*.html")))
        self.resolve_post_links()
        self._export_json()
        log.info("Archive repair complete: cleaned %d page(s)", repaired)
        return repaired

    # ------------------------------------------------------------------
    # JSON export
    # ------------------------------------------------------------------

    def _export_json(self) -> None:
        """Build a nested JSON structure from saved HTML files and write forum.json."""
        log.info("Building JSON export from saved pages...")
        structure = _build_forum_structure(self.output_dir)
        json_path = self.output_dir / "forum.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(structure, f, ensure_ascii=False, indent=2)
        log.info("JSON export written to %s", json_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive forum visio.getbb.ru to a local static copy."
    )
    parser.add_argument("--version", action="version", version=f"ForumArchiver {PARSER_VERSION}")
    parser.add_argument(
        "-o",
        "--output",
        default="forum_archive",
        help="Output directory (default: forum_archive)",
    )
    parser.add_argument(
        "-d",
        "--delay",
        type=float,
        default=0.1,
        help="Minimum delay in seconds between HTTP requests (default: 0.1)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum number of HTML pages to save (0 = unlimited)",
    )
    parser.add_argument(
        "--start-url",
        default=BASE_URL,
        help=f"Starting URL (default: {BASE_URL})",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip pages already saved to the output directory (resume an interrupted download)",
    )
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=READ_TIMEOUT,
        help=(
            "Seconds to wait for response data before treating a request as "
            f"stalled (default: {READ_TIMEOUT:g})"
        ),
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=CONNECT_TIMEOUT,
        help=f"Seconds to wait for connection (default: {CONNECT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--retry-passes",
        type=int,
        default=RETRY_PASSES,
        help=(
            "Deferred passes over temporarily failed pages and downloads after the crawl "
            f"(0 = disabled, default: {RETRY_PASSES})"
        ),
    )
    parser.add_argument(
        "--resolve-post-links",
        action="store_true",
        help="Rebuild posts.json and repair quoted-post links in an existing archive without HTTP requests",
    )
    parser.add_argument(
        "--repair-archive",
        action="store_true",
        help="Repair saved headers, footers and post links, then rebuild JSON without HTTP requests",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    archiver = ForumParser(
        output_dir=args.output,
        delay=args.delay,
        max_pages=args.max_pages,
        resume=args.resume,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        retry_passes=args.retry_passes,
    )
    if args.repair_archive or args.resolve_post_links:
        if not archiver.output_dir.is_dir():
            parser.error("offline repair requires an existing output directory")
        if args.repair_archive:
            archiver.repair_archive()
        else:
            archiver.resolve_post_links()
        return
    archiver.crawl(start_url=args.start_url)


if __name__ == "__main__":
    main()
