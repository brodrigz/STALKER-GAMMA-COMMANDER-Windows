"""Read xrRazom release announcements without scraping protected addon pages."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.request import Request

from .network import read_response_bytes, urlopen_with_retry

NEWS_URL = "https://www.moddb.com/mods/xrrazom-stalker-anomaly-co-op/articles"
NEWS_FEED = "https://rss.moddb.com/mods/xrrazom-stalker-anomaly-co-op/articles/feed/rss.xml"
_NEWS_PATH = "/mods/xrrazom-stalker-anomaly-co-op/news/"
_MAX_BYTES = 512 * 1024


@dataclass(frozen=True)
class CoopRelease:
    version: str
    title: str
    summary: str
    date: str
    url: str


class _SummaryText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        elif tag in {"br", "p", "li", "div"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        elif tag in {"p", "li", "div"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def version_key(version: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
        raise ValueError("Unrecognized xrRazom version.")
    values = tuple(int(part) for part in version.split("."))
    return values + (0,) * (4 - len(values))


def parse_releases(data: bytes) -> list[CoopRelease]:
    if len(data) > _MAX_BYTES or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", data, re.IGNORECASE):
        raise ValueError("Invalid xrRazom release feed.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("Could not read the xrRazom release feed.") from exc
    releases = {}
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        match = re.search(r"\bxrRazom\s*:?\s*(?:version\s+|v)(\d+(?:\.\d+){1,3})\b", title, re.IGNORECASE)
        if not match or re.search(r"\b(?:alpha|beta|preview|upcoming|planned|teaser|rc\d*)\b|pre-release", title, re.IGNORECASE):
            continue
        url = (item.findtext("link") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.moddb.com" or not parsed.path.startswith(_NEWS_PATH):
            continue
        summary = _SummaryText()
        summary.feed(item.findtext("description") or "")
        text = re.sub(r"\s+", " ", "".join(summary.parts)).strip()
        date = ""
        try:
            date = parsedate_to_datetime(item.findtext("pubDate") or "").strftime("%Y-%m-%d")
        except (ValueError, TypeError, OverflowError):
            pass
        version = match.group(1)
        releases.setdefault(version_key(version), CoopRelease(version, title, text, date, url))
    if not releases:
        raise ValueError("No xrRazom release announcements found. Open the official release page to check manually.")
    return [releases[key] for key in sorted(releases, reverse=True)][:10]


def check_coop_releases() -> list[CoopRelease]:
    request = Request(NEWS_FEED, headers={"User-Agent": "STALKER-GAMMA-COMMANDER", "Accept": "application/rss+xml"})
    with urlopen_with_retry(request, timeout=15, attempts=2) as response:
        return parse_releases(read_response_bytes(response, _MAX_BYTES))
