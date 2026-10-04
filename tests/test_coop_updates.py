from xml.sax.saxutils import escape

import pytest

from commander_gui.coop_updates import parse_releases, version_key


def item(title, description="Changes", url=None):
    url = url or "https://www.moddb.com/mods/xrrazom-stalker-anomaly-co-op/news/release"
    return (
        f"<item><title>{escape(title)}</title><link>{escape(url)}</link>"
        f"<description>{escape(description)}</description>"
        "<pubDate>Sat, 03 Oct 2026 18:17:05 +0000</pubDate></item>"
    )


def feed(*items):
    return ("<rss><channel>" + "".join(items) + "</channel></rss>").encode()


def test_release_history_sorts_versions_and_ignores_previews_and_foreign_links():
    releases = parse_releases(feed(
        item("xrRazom: Version 1.1 - Fixes"),
        item("xrRazom v1.9 - Changes"),
        item("xrRazom v1.10 - Changes"),
        item("xrRazom v1.10.0 - Duplicate"),
        item("xrRazom v2.0 preview"),
        item("xrRazom v3.0", url="https://example.com/news/release"),
        item("Other mod v4.0"),
    ))
    assert [release.version for release in releases] == ["1.10", "1.9", "1.1"]
    assert releases[0].date == "2026-10-03"
    assert version_key("1.4") == version_key("1.4.0")


def test_summary_is_plain_text_without_remote_images_or_scripts():
    release = parse_releases(feed(item(
        "xrRazom v1.4", '<img src="https://example.com/image"><p>Fixes &amp; changes</p>'
        '<script>unwanted()</script><style>hidden</style><p>More details</p>',
    )))[0]
    assert release.summary == "Fixes & changes More details"


@pytest.mark.parametrize("data", [
    b"<html><title>Just a moment...</title></html>",
    b"Too Many Requests", b"<rss><channel/></rss>",
    b'<!DOCTYPE rss [<!ENTITY x "test">]><rss/>', b"x" * (512 * 1024 + 1),
], ids=["challenge", "rate-limit", "empty", "doctype", "oversized"])
def test_bad_feed_never_reports_up_to_date(data):
    with pytest.raises(ValueError):
        parse_releases(data)
