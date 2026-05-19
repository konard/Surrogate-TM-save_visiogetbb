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
    elif not os.path.splitext(path)[1]:
        path = path + ".html"

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


class ForumParser:
    def __init__(self, output_dir: str, delay: float = 1.0, max_pages: int = 0):
        self.output_dir = Path(output_dir)
        self.delay = delay
        self.max_pages = max_pages  # 0 = unlimited
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

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def fetch(self, url: str) -> requests.Response | None:
        try:
            resp = self.session.get(url, timeout=30, allow_redirects=True)
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
        soup = BeautifulSoup(html, "html.parser")

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

        log.info("[%d] Fetching: %s", self.pages_saved + 1, norm)
        resp = self.fetch(norm)
        if resp is None:
            return

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            log.debug("Skipping non-HTML content at %s", norm)
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
    )
    archiver.crawl(start_url=args.start_url)


if __name__ == "__main__":
    main()
