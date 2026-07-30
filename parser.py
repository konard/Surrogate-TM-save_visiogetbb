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
from bs4 import BeautifulSoup
from bs4.formatter import HTMLFormatter

BASE_URL = "https://visio.getbb.ru"

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
    ]

    FOOTER_FORM_NAMES = {"search", "jumpbox"}

    def _is_footer_table(tag) -> bool:
        if tag.name != "table":
            return False
        # Check for known footer form names
        for form in tag.find_all("form"):
            if form.get("name") in FOOTER_FORM_NAMES:
                return True
        text = tag.get_text(" ", strip=True)
        return any(sig in text for sig in FOOTER_TABLE_SIGNATURES)

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


class ForumParser:
    def __init__(self, output_dir: str, delay: float = 1.0, max_pages: int = 0, resume: bool = False):
        self.output_dir = Path(output_dir)
        self.delay = delay
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
        self.downloaded_files: dict[str, Path] = {}  # url -> local path
        self.queue: deque[str] = deque()
        self.pages_saved = 0
        self._download_log_handler: logging.FileHandler | None = None
        self._download_log: logging.Logger | None = None

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

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def fetch(self, url: str) -> requests.Response | None:
        try:
            resp = self.session.get(url, timeout=60, allow_redirects=True)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            log.warning("Failed to fetch %s: %s", url, e)
            return None

    # ------------------------------------------------------------------
    # File download helpers
    # ------------------------------------------------------------------

    def download_file(self, url: str) -> Path | None:
        """Download a binary file and return its local path."""
        norm = normalize_url(url)
        if norm in self.downloaded_files:
            return self.downloaded_files[norm]

        resp = self.fetch(url)
        if resp is None:
            self._log_download_fail(url, "fetch failed")
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

        parsed = urlparse(url)
        # Build a safe local path under images/
        # Use host + path to avoid collisions with external images
        host = parsed.netloc.replace(".", "_")
        rel = parsed.path.lstrip("/")
        if not rel:
            rel = "image"
        _, ext = os.path.splitext(rel)

        resp = self.fetch(url)
        if resp is None:
            self._log_download_fail(url, "fetch failed")
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

    def process_page(self, url: str, html: str) -> str:
        """Process HTML: rewrite links, download assets, return modified HTML."""
        local_path = url_to_local_path(normalize_url(url), self.output_dir)
        html = _fix_self_closing_spans(html)
        soup = BeautifulSoup(html, "html.parser")
        _remove_forum_chrome(soup)

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

            # File downloads: download and rewrite
            if "download/file.php" in parsed.path:
                local_file = self.download_file(abs_href)
                if local_file:
                    tag["href"] = self.make_relative(local_path, local_file)
                    time.sleep(self.delay)
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

            # Download user-content images (may be external)
            local_img = self.download_image(abs_src)
            if local_img:
                tag["src"] = self.make_relative(local_path, local_img)
                time.sleep(self.delay * 0.2)

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
            self.pages_saved += 1
            return

        log.info("[%d] Fetching: %s", self.pages_saved + 1, norm)
        resp = self.fetch(norm)
        if resp is None:
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
            return

        processed_html = self.process_page(norm, resp.text)

        local_path = url_to_local_path(norm, self.output_dir)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        with open(local_path, "w", encoding="utf-8") as f:
            f.write(processed_html)

        self.pages_saved += 1
        log.info("Saved: %s -> %s", norm, local_path.relative_to(self.output_dir))
        time.sleep(self.delay)

    # ------------------------------------------------------------------
    # Crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, start_url: str = BASE_URL) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._init_download_log()
        start_norm = normalize_url(start_url)
        self.queue.append(start_norm)

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

        log.info(
            "Crawl complete. Pages saved: %d, Files downloaded: %d",
            self.pages_saved,
            len(self.downloaded_files),
        )
        self._export_json()

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
        default=1.0,
        help="Delay in seconds between requests (default: 1.0)",
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
    )
    archiver.crawl(start_url=args.start_url)


if __name__ == "__main__":
    main()
