import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from commander_gui.moddb_session import (
    ModDbBridge,
    ModDbRateLimitError,
    ModDbSession,
    ModDbSessionError,
    Page,
    parse_response,
    validate_url,
)

START = "https://www.moddb.com/addons/start/306772"
CHALLENGE = Page(403, {"cf-mitigated": "challenge"}, "window._cf_chl_opt = {}")
RATE_LIMIT = """<h1>Too Many Requests</h1><p>It appears you are a bot, hitting our server repeatedly.
If you would like API access and to be whitelisted contact us. Otherwise limit the number of
requests you perform and check back in <b>33mins</b>. Follow us on X or Facebook for the latest updates:</p>"""


@pytest.mark.parametrize("status", [200, 403, 429])
def test_rate_limit_body_stops_requests_without_verification(monkeypatch, status):
    from unittest.mock import Mock

    states = []
    session = ModDbSession(access_changed=states.append)
    fetch = Mock(return_value=Page(status, {}, RATE_LIMIT))
    verify = Mock()
    monkeypatch.setattr(session, "_fetch", fetch)
    monkeypatch.setattr(session, "_wait_for_verification", verify)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [pool.submit(session.request, START) for _ in range(4)]
        for task in tasks:
            with pytest.raises(ModDbRateLimitError, match="33 minutes"):
                task.result(timeout=2)
    # The CLI's repeated calls must also fail locally, even if asking for CF.
    with pytest.raises(ModDbRateLimitError):
        session.request(START, verification_required=True)
    fetch.assert_called_once()
    verify.assert_not_called()
    assert not session.approve_verification()
    assert [s["state"] for s in states] == ["rate_limited"]
    assert "<h1>" not in states[0]["message"]


def test_429_without_body_uses_retry_after_and_unknown_wait_is_not_invented():
    assert "120 seconds" in Page(429, {"retry-after": "120"}, "").rate_limit_message
    assert "Wait before restarting" in Page(429, {}, "").rate_limit_message
    assert Page(200, {}, "Addon discussing Too Many Requests").rate_limit_message is None
    assert CHALLENGE.rate_limit_message is None


def test_rate_limit_after_browser_verification_is_not_treated_as_bad_cookie(monkeypatch):
    states = []
    session = ModDbSession()

    def changed(state):
        states.append(state["state"])
        if state["state"] == "required":
            session.approve_verification()

    async def verify(url):
        session._check_rate_limit(Page(200, {}, RATE_LIMIT))

    session.access_changed = changed
    monkeypatch.setattr(session, "_fetch", lambda _: CHALLENGE)
    monkeypatch.setattr(session, "_verify", verify)
    with pytest.raises(ModDbRateLimitError, match="33 minutes"):
        session.request(START)
    assert states == ["required", "verifying", "rate_limited"]
    assert not session.approve_verification()


def test_rate_limit_disables_full_install_auto_retry():
    from commander_gui.ui.install_page import _should_auto_retry

    output = "Error downloading from Gamma Large Files Repo\nModDB rate limit: too many requests."
    assert not _should_auto_retry(True, 0, output)


def test_single_verification_shared_across_concurrent_addons(monkeypatch):
    session = ModDbSession()
    session.access_changed = lambda state: session.approve_verification() if state["state"] == "required" else None
    calls = []

    def fetch(url):
        return Page(200, {}, "addon") if session._clearance else CHALLENGE

    async def verify(url):
        calls.append(url)
        session._clearance = "fixture"
        session._user_agent = "same browser"

    monkeypatch.setattr(session, "_fetch", fetch)
    monkeypatch.setattr(session, "_verify", verify)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(session.request, [START] * 4))
    assert all(page.body == "addon" for page in pages)
    assert len(calls) == 1
    # A later expired clearance can be refreshed within this same CLI invocation.
    session._clearance = ""
    assert session.request(START).body == "addon"
    assert len(calls) == 2


def test_failed_handoff_waits_for_another_click(monkeypatch):
    session = ModDbSession()
    calls = []
    states = []

    def changed(state):
        states.append(state["state"])
        if state["state"] == "required":
            session.approve_verification()
        elif state["state"] == "failed":
            session.stop()

    session.access_changed = changed

    async def verify(url):
        calls.append(url)

    monkeypatch.setattr(session, "_fetch", lambda url: CHALLENGE)
    monkeypatch.setattr(session, "_verify", verify)
    with pytest.raises(ModDbSessionError, match="cancelled"):
        session.request(START)
    assert len(calls) == 1
    assert states == ["required", "verifying", "failed"]


def test_403_does_not_open_browser_until_button_click(monkeypatch):
    session = ModDbSession()
    waiting = threading.Event()
    calls = []
    states = []

    def changed(state):
        states.append(state)
        if state["state"] == "required":
            waiting.set()

    async def verify(url):
        calls.append(url)
        session._clearance = "private fixture"

    session.access_changed = changed
    monkeypatch.setattr(session, "_fetch", lambda _: Page(200, {}, "addon") if session._clearance else Page(403, {}, "Forbidden"))
    monkeypatch.setattr(session, "_verify", verify)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(session.request, START)
        try:
            assert waiting.wait(2)
            assert calls == []
            assert not task.done()
            assert "403" in states[-1]["message"]
            assert session.approve_verification()
            assert not session.approve_verification()  # double click cannot authorize another browser
            assert task.result(timeout=2).body == "addon"
        finally:
            session.stop()
    assert states[-1]["state"] == "ready"
    assert "private fixture" not in json.dumps(states)


def test_archive_403_requires_click_even_when_pages_accessible(monkeypatch):
    session = ModDbSession()
    waiting = threading.Event()
    session.access_changed = lambda state: waiting.set() if state["state"] == "required" else None
    monkeypatch.setattr(session, "_fetch", lambda _: Page(200, {}, "addon"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(session.request, START, False, True)
        try:
            assert waiting.wait(2)
            assert not task.done()
        finally:
            session.stop()
        with pytest.raises(ModDbSessionError, match="cancelled"):
            task.result(timeout=2)


@pytest.mark.parametrize("url", ["https://evil.test/addons/x", "http://www.moddb.com/addons/x",
                                 "https://www.moddb.com@evil.test/addons/x", "https://www.moddb.com:8443/addons/x",
                                 "https://www.moddb.com/login", START + "\r\nx: y"])
def test_restricts_cookie_origin_and_paths(url):
    with pytest.raises(ModDbSessionError):
        validate_url(url)


def test_redirect_headers_return_without_contacting_cdn(monkeypatch):
    session = ModDbSession()
    requested = []

    def fetch(url):
        requested.append(url)
        return Page(302, {"location": "https://cdn.example/archive.zip", "set-cookie": "secret"}, "")

    monkeypatch.setattr(session, "_fetch", fetch)
    assert session.request(START, headers_only=True).headers == {"location": "https://cdn.example/archive.zip"}
    with pytest.raises(ModDbSessionError, match="Unsupported"):
        session.request(START)
    assert requested == [START, START]


def test_cancellation_closes_verification_coroutine(monkeypatch):
    session = ModDbSession()
    cleaned = []

    async def verify(url):
        try:
            session.stop()
            await asyncio.sleep(10)
        finally:
            cleaned.append(True)

    monkeypatch.setattr(session, "_verify", verify)
    with pytest.raises(ModDbSessionError, match="cancelled"):
        asyncio.run(session._verify_until_stopped(START))
    assert cleaned == [True]


def test_bridge_auth_response_and_cleanup(monkeypatch):
    session = ModDbSession()
    monkeypatch.setattr(session, "request", lambda url, headers, required: Page(200, {}, "addon HTML"))
    bridge = ModDbBridge(session=session)
    env = bridge.start()
    opener = build_opener(ProxyHandler({}))
    try:
        request = Request(env["COMMANDER_MODDB_BRIDGE"], json.dumps({"url": START}).encode())
        with pytest.raises(HTTPError) as error:
            opener.open(request)
        assert error.value.code == 403
        request.add_header("X-Commander-Token", env["COMMANDER_MODDB_TOKEN"])
        with opener.open(request) as response:
            assert json.load(response)["body"] == "addon HTML"
    finally:
        bridge.close()
    assert not bridge.thread.is_alive()
    assert session.stopped.is_set()


def test_parse_challenge_and_interim_responses():
    page = parse_response(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/2 403\r\nCf-Mitigated: challenge\r\n\r\nblocked")
    assert page.challenged
    assert page.status == 403


def test_cloudflare_js_detection_script_is_not_a_challenge():
    page = Page(200, {}, '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>')
    assert not page.challenged


def test_cancel_while_waiting_for_other_page(monkeypatch):
    session = ModDbSession()
    session._lock.acquire()
    results = []

    def request():
        try:
            session.request(START)
        except ModDbSessionError as exc:
            results.append(str(exc))

    thread = threading.Thread(target=request)
    thread.start()
    session.stop()
    session._lock.release()
    thread.join(timeout=1)
    assert results == ["ModDB request cancelled."]
