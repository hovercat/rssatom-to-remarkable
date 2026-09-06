#!/usr/bin/env python3
"""Fetch RSS feeds, generate PDFs, upload to reMarkable Cloud."""

import base64
import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Vienna")

import feedparser
import pymupdf
import qrcode
import requests
from readability import Document
from weasyprint import CSS, HTML

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = BASE_DIR / "cache"
STATE_FILE = BASE_DIR / "seen.json"
CONFIG_FILE = BASE_DIR / "input.toml"
REMARKABLE_ROOT = "/rss"

GERMAN_WEEKDAYS = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]

PAGE_WIDTH_MM = 157

PDF_CSS = """
body {
    font-family: serif;
    font-size: 9.5pt;
    line-height: 1.5;
    color: #000;
    text-align: justify;
    hyphens: auto;
    -webkit-hyphens: auto;
}
h1.feed-title {
    font-size: 17pt;
    margin-bottom: 4pt;
}
.feed-date {
    font-size: 8.5pt;
    color: #444;
    margin-bottom: 16pt;
}
.toc {
    margin-bottom: 20pt;
    padding: 10pt;
    border: 1px solid #000;
}
.toc h2 {
    font-size: 12pt;
    margin: 0 0 8pt 0;
}
.toc ol {
    margin: 0;
    padding-left: 20pt;
}
.toc li {
    font-size: 8.5pt;
    line-height: 1.8;
}
.toc-meta {
    font-size: 7pt;
    color: #666;
}
.article-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 8pt;
}
.article-header-text {
    flex: 1;
    padding-right: 10pt;
}
.article-title {
    font-size: 13pt;
    font-weight: bold;
    margin: 0 0 4pt 0;
}
.article-meta {
    font-size: 7.5pt;
    color: #444;
    margin-bottom: 4pt;
}
.article-qr {
    flex-shrink: 0;
}
.article-qr img {
    width: 25mm;
    height: 25mm;
}
.article-body {
    margin-top: 8pt;
}
.article-body img {
    max-width: 100%;
    height: auto;
}
a {
    color: #000;
    text-decoration: underline;
}
.inline-qr {
    height: 1.4em;
    width: 1.4em;
    vertical-align: middle;
    margin-left: 1pt;
}
"""


def make_page_css(width_mm: int, height_mm: int) -> str:
    return f"@page {{ size: {width_mm}mm {height_mm}mm; margin: 10mm 12mm; }}"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def article_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def make_qr_data_uri(url: str, box_size: int = 4) -> str:
    """Generate a QR code as a base64 PNG data URI."""
    qr = qrcode.QRCode(box_size=box_size, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def make_inline_qr(url: str) -> str:
    """Generate a tiny inline QR code img tag matching text height."""
    uri = make_qr_data_uri(url, box_size=2)
    return f'<img class="inline-qr" src="{uri}" alt="QR">'


def inject_link_qr_codes(html: str) -> str:
    """Append a tiny QR code after every <a href="http..."> link in the HTML."""
    def replace_link(m):
        full_match = m.group(0)
        href = m.group(1)
        if href.startswith(("http://", "https://")):
            return full_match + make_inline_qr(href)
        return full_match

    return re.sub(r'<a\s+href="([^"]+)"[^>]*>.*?</a>', replace_link, html, flags=re.DOTALL)


def sanitize_html(html: str) -> str:
    """Remove problematic Unicode characters that break PDF rendering."""
    return re.sub(r'[\u2028\u2029\u0080-\u009f]', '', html)


def strip_body_ids(html: str) -> str:
    """Strip id attributes from article body HTML to prevent anchor conflicts."""
    return re.sub(r'\s+id="[^"]*"', '', html)


def parse_feeds(config: dict) -> list[dict]:
    """Walk the TOML config and return feed definitions."""
    feeds = []

    def walk(node: dict, path: list[str]) -> None:
        for key, value in node.items():
            if isinstance(value, str):
                category = "/".join(path) if path else "uncategorized"
                feeds.append({"category": category, "name": key, "url": value})
            elif isinstance(value, dict):
                walk(value, path + [key])

    walk(config, [])
    return feeds


def fetch_feed(url: str) -> list[dict]:
    """Fetch an RSS feed and return its entries."""
    log.info("Fetching feed: %s", url)
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; RSS2Remarkable/1.0)"
        })
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:
        log.warning("Could not fetch feed %s: %s", url, e)
        return []
    if feed.bozo and not feed.entries:
        log.warning("Feed parse error for %s: %s", url, feed.bozo_exception)
        return []
    return feed.entries


def fetch_article_content(url: str) -> str:
    """Fetch a web page and extract readable content, with disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(url.encode()).hexdigest()[:16]
    cache_file = CACHE_DIR / f"{cache_key}.html"

    if cache_file.exists():
        return cache_file.read_text()

    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; RSS2Remarkable/1.0)"
        })
        resp.raise_for_status()
        doc = Document(resp.text)
        content = doc.summary()
        cache_file.write_text(content)
        return content
    except Exception as e:
        log.warning("Could not fetch article %s: %s", url, e)
        return ""


def extract_date(published: str) -> str:
    """Extract just the date (YYYY-MM-DD) from an RSS published string."""
    if not published:
        return ""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(published)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    # Try ISO format fallback
    m = re.match(r"(\d{4}-\d{2}-\d{2})", published)
    return m.group(1) if m else ""


def build_toc_html(feed_name: str, articles: list[dict]) -> str:
    """Build a TOC page for a feed."""
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    parts = [
        "<html><head><meta charset='utf-8'></head><body>",
        f'<h1 class="feed-title">{feed_name}</h1>',
        f'<div class="feed-date">{today}</div>',
        '<div class="toc">',
        "<h2>Contents</h2>",
        "<ol>",
    ]
    for art in articles:
        title = art.get("title", "Untitled")
        date_str = extract_date(art.get("published", ""))
        meta_str = f' <span class="toc-meta">({date_str})</span>' if date_str else ""
        parts.append(f"<li>{title}{meta_str}</li>")
    parts.append("</ol></div></body></html>")
    return sanitize_html("\n".join(parts))


def build_article_html(art: dict) -> str:
    """Build a single article page."""
    title = art.get("title", "Untitled")
    link = art.get("link", "")
    published = art.get("published", "")
    content = art.get("content", "")

    if not content:
        summary = art.get("summary", "")
        content = summary if summary else "<p><em>No content available.</em></p>"

    content = strip_body_ids(content)
    content = inject_link_qr_codes(content)
    qr_uri = make_qr_data_uri(link) if link else ""

    parts = ["<html><head><meta charset='utf-8'></head><body>"]
    parts.append('<div class="article-header">')
    parts.append('<div class="article-header-text">')
    parts.append(f'<div class="article-title">{title}</div>')
    author = art.get("author", "")
    feed_source = art.get("feed", "")
    meta_parts = []
    if feed_source:
        meta_parts.append(feed_source)
    if published:
        meta_parts.append(published)
    if author:
        meta_parts.append(author)
    if link:
        meta_parts.append(link)
    if meta_parts:
        parts.append(f'<div class="article-meta">{" | ".join(meta_parts)}</div>')
    parts.append("</div>")
    if qr_uri:
        parts.append(f'<div class="article-qr"><img src="{qr_uri}" alt="QR"></div>')
    parts.append("</div>")
    parts.append(f'<div class="article-body">{content}</div>')
    parts.append("</body></html>")
    return sanitize_html("\n".join(parts))


def render_single_page_pdf(html: str, output_path: Path) -> bool:
    """Render HTML into a single tall page PDF (two-pass for height)."""
    try:
        measure_css = CSS(string=make_page_css(PAGE_WIDTH_MM, 210))
        body_css = CSS(string=PDF_CSS)
        doc = HTML(string=html).render(stylesheets=[measure_css, body_css])
        total_height_pt = sum(page.height for page in doc.pages)
        total_height_mm = int(total_height_pt * 0.3528) + 5
        single_css = CSS(string=make_page_css(PAGE_WIDTH_MM, total_height_mm))
        HTML(string=html).write_pdf(
            str(output_path), stylesheets=[single_css, body_css]
        )
        return True
    except Exception as e:
        log.error("PDF render failed: %s", e)
        return False


def add_bookmarks_and_toc_links(pdf_path: Path, outline: list[dict]) -> None:
    """Add PDF outline (bookmarks) and TOC page links.

    outline: list of {title, feed, toc_page, article_pages: [(page_idx, title)]}
    """
    doc = pymupdf.open(str(pdf_path))

    # Build PDF table of contents (bookmarks)
    toc = []
    for feed in outline:
        # Level 1: feed name pointing to its TOC page
        toc.append([1, feed["feed"], feed["toc_page"] + 1])
        for page_idx, title in feed["article_pages"]:
            # Level 2: article title pointing to its page
            toc.append([2, title, page_idx + 1])

    doc.set_toc(toc)

    # Add clickable links on each TOC page
    for feed in outline:
        toc_page = doc[feed["toc_page"]]
        for page_idx, title in feed["article_pages"]:
            hits = toc_page.search_for(title) or []
            if hits:
                toc_page.insert_link({
                    "kind": pymupdf.LINK_GOTO,
                    "from": hits[0],
                    "to": pymupdf.Point(0, 0),
                    "page": page_idx,
                })

    tmp_path = str(pdf_path) + ".tmp"
    doc.save(tmp_path, encryption=0)
    doc.close()
    Path(tmp_path).replace(pdf_path)
    log.info("Added bookmarks and TOC links to %s", pdf_path.name)


def rmapi_cmd(*args: str) -> subprocess.CompletedProcess:
    """Run an rmapi command."""
    result = subprocess.run(
        ["rmapi", *args],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        log.error("rmapi %s failed: %s", " ".join(args), result.stderr)
    return result


def ensure_remarkable_folder(folder_path: str) -> None:
    """Create folder hierarchy on reMarkable if it doesn't exist."""
    parts = folder_path.strip("/").split("/")
    current = ""
    for part in parts:
        current += f"/{part}"
        rmapi_cmd("mkdir", current)


def upload_to_remarkable(pdf_path: Path, remote_folder: str) -> bool:
    """Upload a PDF to a specific folder on reMarkable Cloud."""
    ensure_remarkable_folder(remote_folder)
    result = rmapi_cmd("put", str(pdf_path), remote_folder)
    if result.returncode == 0:
        log.info("Uploaded %s to %s", pdf_path.name, remote_folder)
        return True
    return False


def collect_feed_articles(
    feed_info: dict, state: dict, max_articles: int = 15
) -> list[dict]:
    """Fetch a feed and return all current entries (using cache for content)."""
    name = feed_info["name"]
    category = feed_info["category"]
    url = feed_info["url"]

    entries = fetch_feed(url)
    if not entries:
        log.warning("No entries for %s", name)
        return []

    articles = []
    new_count = 0
    for entry in entries[:max_articles]:
        link = entry.get("link", "")
        if not link:
            continue

        aid = article_id(link)
        is_new = aid not in state

        content = fetch_article_content(link)
        published = entry.get("published", "") or entry.get("updated", "")
        author = entry.get("author", "")
        articles.append({
            "title": entry.get("title", "Untitled"),
            "link": link,
            "published": published,
            "author": author,
            "summary": entry.get("summary", ""),
            "content": content,
            "feed": f"{category}/{name}",
        })

        if is_new:
            new_count += 1
            state[aid] = {
                "url": link,
                "title": entry.get("title", ""),
                "fetched": datetime.now(LOCAL_TZ).isoformat(),
            }

    log.info("Found %d articles (%d new) for %s/%s",
             len(articles), new_count, category, name)
    return articles


def build_feed_pdf(feed_info: dict, articles: list[dict], output_path: Path) -> bool:
    """Build a PDF for one feed: TOC page + one page per article."""
    feed_label = f"{feed_info['category']}/{feed_info['name']}"

    with tempfile.TemporaryDirectory() as tmpdir:
        part_files = []
        article_pages = []

        # TOC page
        toc_html = build_toc_html(feed_label, articles)
        toc_path = Path(tmpdir) / "toc.pdf"
        if not render_single_page_pdf(toc_html, toc_path):
            return False
        part_files.append(toc_path)

        # One page per article
        for i, art in enumerate(articles):
            art_html = build_article_html(art)
            art_path = Path(tmpdir) / f"art_{i}.pdf"
            if render_single_page_pdf(art_html, art_path):
                part_files.append(art_path)
                article_pages.append((len(part_files) - 1, art.get("title", "Untitled")))

        # Merge
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if len(part_files) == 1:
            shutil.copy2(part_files[0], output_path)
        else:
            result = subprocess.run(
                ["pdfunite", *[str(p) for p in part_files], str(output_path)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                log.error("pdfunite failed for %s: %s", feed_label, result.stderr)
                return False

        # Add bookmarks and TOC links
        feed_outline = [{"feed": feed_label, "toc_page": 0, "article_pages": article_pages}]
        add_bookmarks_and_toc_links(output_path, feed_outline)

        log.info("Built %s: 1 TOC + %d articles -> %s",
                 feed_label, len(article_pages), output_path.name)
        return True


def main() -> None:
    log.info("Starting RSS to reMarkable sync")

    with open(CONFIG_FILE, "rb") as f:
        config = tomllib.load(f)

    feeds = parse_feeds(config)
    log.info("Found %d feeds in config", len(feeds))

    state = load_state()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(LOCAL_TZ)
    weekday = GERMAN_WEEKDAYS[now.weekday()]
    date_folder = now.strftime("%Y-%m-%d-%H%M") + f"-{weekday}"

    for feed_info in feeds:
        articles = collect_feed_articles(feed_info, state)
        if not articles:
            continue

        category = feed_info["category"]
        filename = f"{feed_info['name']}.pdf"
        pdf_path = OUTPUT_DIR / date_folder / category / filename

        if build_feed_pdf(feed_info, articles, pdf_path):
            remote_folder = f"{REMARKABLE_ROOT}/{date_folder}/{category}"
            upload_to_remarkable(pdf_path, remote_folder)

    save_state(state)
    log.info("Done")


if __name__ == "__main__":
    main()
