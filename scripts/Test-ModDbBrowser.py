"""Read-only browser verification / native curl handoff experiment.

Run with .venv/Scripts/python.exe scripts/Test-ModDbBrowser.py.
Uses an isolated browser profile under ignored build/, never a personal profile.
Only request metadata pages; no addon downloads or game files are changed.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket

ROOT = Path(__file__).resolve().parents[1]


def wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the browser")
        QCoreApplication.processEvents()
        time.sleep(0.05)


class DevTools:
    def __init__(self, url: str) -> None:
        self.socket = QWebSocket()
        self.replies: dict[int, dict] = {}
        self.sequence = 0
        self.socket.textMessageReceived.connect(self._receive)
        self.socket.open(QUrl(url))
        wait_until(lambda: self.socket.state() == QAbstractSocket.SocketState.ConnectedState, 15)

    def _receive(self, message: str) -> None:
        reply = json.loads(message)
        if "id" in reply:
            self.replies[reply["id"]] = reply

    def call(self, method: str, params: dict | None = None) -> dict:
        self.sequence += 1
        identifier = self.sequence
        self.socket.sendTextMessage(json.dumps({"id": identifier, "method": method, "params": params or {}}))
        wait_until(lambda: identifier in self.replies, 30)
        reply = self.replies.pop(identifier)
        if "error" in reply:
            raise RuntimeError(f"Browser command failed: {method}: {reply['error'].get('message', '')}")
        return reply.get("result", {})

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            raise RuntimeError("Browser page evaluation failed")
        return result.get("result", {}).get("value")


class NativeCurl:
    """Use the same native library and impersonation profile as the CLI."""

    def __init__(self) -> None:
        self.library = ctypes.CDLL(str(ROOT / "cli/windows/libcurl-impersonate.dll"))
        lib = self.library
        lib.curl_global_init.argtypes = [ctypes.c_long]
        lib.curl_global_init.restype = ctypes.c_int
        if lib.curl_global_init(3):
            raise RuntimeError("curl initialization failed")
        lib.curl_easy_init.restype = ctypes.c_void_p
        lib.curl_easy_impersonate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.curl_easy_impersonate.restype = ctypes.c_int
        # curl's variadic API requires typed arguments at each invocation.
        lib.curl_easy_setopt.restype = ctypes.c_int
        lib.curl_easy_perform.argtypes = [ctypes.c_void_p]
        lib.curl_easy_perform.restype = ctypes.c_int
        lib.curl_easy_getinfo.restype = ctypes.c_int
        lib.curl_easy_cleanup.argtypes = [ctypes.c_void_p]

    def fetch(self, url: str, *, cookies: list[dict] | None = None, user_agent: str | None = None) -> dict:
        lib = self.library
        handle = lib.curl_easy_init()
        if not handle:
            raise RuntimeError("curl request initialization failed")
        body: list[bytes] = []
        headers: list[bytes] = []
        callback_type = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p)

        def receive(destination):
            def callback(data, size, count, unused):
                length = size * count
                destination.append(ctypes.string_at(data, length))
                return length
            return callback_type(callback)

        body_callback, header_callback = receive(body), receive(headers)
        strings: list[bytes] = []

        def option(key, value):
            if isinstance(value, str):
                strings.append(value.encode("utf-8"))
                value = ctypes.c_char_p(strings[-1])
            elif isinstance(value, int):
                value = ctypes.c_long(value)
            if lib.curl_easy_setopt(ctypes.c_void_p(handle), ctypes.c_int(key), value):
                raise RuntimeError(f"curl option {key} failed")

        try:
            if lib.curl_easy_impersonate(handle, b"chrome145", 1):
                raise RuntimeError("curl browser profile unavailable")
            option(10002, url)  # URL
            option(10065, str(ROOT / "cli/windows/cacert.pem"))
            option(10102, "")  # automatic content decoding
            option(52, 1)  # redirects
            option(84, 30)  # HTTP/3 preference, as in the CLI
            option(13, 30)  # total timeout
            option(78, 10)  # connect timeout
            option(20011, body_callback)
            option(20079, header_callback)
            # Cookie engine enforces domain/path/secure rules across redirects.
            option(10031, "")
            for cookie in cookies or []:
                domain = cookie["domain"]
                if domain.lstrip(".").lower() not in {"moddb.com", "www.moddb.com"}:
                    continue
                fields = [domain, "TRUE" if domain.startswith(".") else "FALSE", cookie.get("path", "/"),
                          "TRUE" if cookie.get("secure") else "FALSE", str(max(0, int(cookie.get("expires", 0)))),
                          cookie["name"], cookie["value"]]
                if any("\t" in field or "\n" in field or "\r" in field for field in fields):
                    continue
                option(10135, "\t".join(fields))  # COOKIELIST
            if user_agent:
                option(10018, user_agent)
            code = lib.curl_easy_perform(handle)
            if code:
                raise RuntimeError(f"curl transfer failed with code {code}")
            status = ctypes.c_long()
            lib.curl_easy_getinfo(ctypes.c_void_p(handle), ctypes.c_int(0x200002), ctypes.byref(status))
            text = b"".join(body).decode("utf-8", errors="replace")
            response_headers = b"".join(headers).decode("latin-1").lower()
            challenge = "cf-mitigated: challenge" in response_headers or "window._cf_chl_opt" in text
            return {"status": status.value, "challenge": challenge,
                    "canonical_found": 'rel="canonical"' in text or "rel='canonical'" in text,
                    "body_bytes": sum(map(len, body))}
        finally:
            lib.curl_easy_cleanup(handle)


def find_browser() -> Path:
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if not root:
            continue
        for relative in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe",
                         "BraveSoftware/Brave-Browser/Application/brave.exe"):
            candidate = Path(root) / relative
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("Install Edge, Chrome or Brave, or provide --browser.")


def check_browser_requests(page: DevTools, urls: list[str]) -> list[dict]:
    """Fetch normal page responses in the session the user just verified."""
    results = []
    for url in urls:
        expression = """(async () => {
            const r = await fetch(URL_PLACEHOLDER, {credentials:'include', signal:AbortSignal.timeout(20000)});
            const text = await r.text();
            const doc = new DOMParser().parseFromString(text, 'text/html');
            return {status:r.status, challenge:r.headers.get('cf-mitigated') === 'challenge' || text.includes('window._cf_chl_opt'),
                    canonical:doc.querySelector('link[rel=canonical]')?.href || null,
                    metadata_present:text.includes('MD5 Hash') && text.includes('Filename'), body_chars:text.length};
        })()""".replace("URL_PLACEHOLDER", json.dumps(url))
        try:
            result = page.evaluate(expression)
        except (RuntimeError, TimeoutError) as exc:
            result = {"error": str(exc)}
        results.append({"url": url, **result})
        time.sleep(1)
    return results


def check_browser_navigation(page: DevTools, urls: list[str], timeout: int) -> list[dict]:
    results = []
    for url in urls:
        print("Opening page in the verified browser:", url, flush=True)
        page.call("Page.navigate", {"url": url})
        deadline = time.monotonic() + timeout
        state = {}
        while time.monotonic() < deadline:
            state = page.evaluate("({url:location.href,title:document.title,ready:document.readyState,canonical:document.querySelector('link[rel=canonical]')?.href,challenge:!!window._cf_chl_opt,metadata_present:document.body?.innerText.includes('MD5 Hash') && document.body?.innerText.includes('Filename'),mirror_links:document.querySelectorAll('a#downloadon').length})") or {}
            current = urlsplit(state.get("url", ""))
            expected = urlsplit(url)
            if current.hostname == expected.hostname and current.path == expected.path and state.get("ready") == "complete" and not state.get("challenge") and (state.get("canonical") or state.get("metadata_present") or state.get("mirror_links")):
                state["succeeded"] = True
                break
            QCoreApplication.processEvents()
            time.sleep(1)
        else:
            state["succeeded"] = False
        # Don't retain challenge query strings in the report.
        state.pop("url", None)
        results.append({"url": url, **state})
        print("Browser navigation:", json.dumps(results[-1]), flush=True)
        if not state["succeeded"]:
            break
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--url", default="https://www.moddb.com/addons/start/306772")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--navigation-only", action="store_true", help="Keep every request in the visible browser; do not export cookies or run curl")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme != "https" or parsed.hostname != "www.moddb.com" or parsed.username or parsed.password:
        parser.error("This probe accepts only HTTPS www.moddb.com pages")
    app = QCoreApplication([])
    assert app is not None
    run = ROOT / "build/moddb-browser-probe" / uuid.uuid4().hex
    profile = run / "profile"
    profile.mkdir(parents=True)
    report: dict = {"url": args.url, "mode": "navigation" if args.navigation_only else "cookie-handoff"}
    browser = None
    page = None
    try:
        curl = None if args.navigation_only else NativeCurl()
        if curl:
            report["curl_before"] = curl.fetch(args.url)
            print("Native curl before verification:", json.dumps(report["curl_before"]), flush=True)
        browser = subprocess.Popen([str(args.browser or find_browser()), f"--user-data-dir={profile}",
                                    "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                                    "--no-first-run", "--no-default-browser-check", "--new-window", "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = profile / "DevToolsActivePort"
        wait_until(lambda: port_file.is_file() and port_file.stat().st_size > 0, 25)
        port = int(port_file.read_text().splitlines()[0])
        local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with local_http.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            tabs = json.load(response)
        tab = next(tab for tab in tabs if tab["type"] == "page" and tab["url"] == "about:blank")
        page = DevTools(tab["webSocketDebuggerUrl"])
        page.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        page.call("Page.navigate", {"url": args.url})
        print("Browser opened. Complete any ModDB verification in that window; the probe will resume automatically.", flush=True)
        deadline = time.monotonic() + args.timeout
        state = {}
        while time.monotonic() < deadline:
            state = page.evaluate("({url:location.href,title:document.title,ready:document.readyState,canonical:document.querySelector('link[rel=canonical]')?.href,challenge:!!window._cf_chl_opt})") or {}
            if urlsplit(state.get("url", "")).hostname == "www.moddb.com" and state.get("canonical") and not state.get("challenge"):
                break
            QCoreApplication.processEvents()
            time.sleep(1)
        else:
            report["browser"] = {"verified": False, "title": state.get("title", ""), "challenge": state.get("challenge", False)}
            print("Browser verification was not completed before the timeout.", flush=True)
            return 2
        report["browser"] = {"verified": True, "title": state.get("title", ""), "canonical": state.get("canonical")}
        print("Browser reached the addon page.", flush=True)
        if args.navigation_only:
            report["browser_navigation"] = check_browser_navigation(page, [state["canonical"], args.url + "/all", "https://www.moddb.com/addons/start/300660"], args.timeout)
            report["navigation_succeeded"] = all(item["succeeded"] for item in report["browser_navigation"])
            return 0 if report["navigation_succeeded"] else 4
        assert curl is not None
        cookies = page.call("Network.getCookies", {"urls": [args.url]})["cookies"]
        user_agent = page.evaluate("navigator.userAgent")
        report["clearance_cookie_present"] = any(cookie["name"] == "cf_clearance" for cookie in cookies)
        report["curl_after"] = curl.fetch(args.url, cookies=cookies, user_agent=user_agent)
        report["handoff_succeeded"] = report["curl_after"]["status"] == 200 and report["curl_after"]["canonical_found"] and not report["curl_after"]["challenge"]
        print("Native curl after verification:", json.dumps(report["curl_after"]), flush=True)
        print("Cookie handoff accepted:", report["handoff_succeeded"], flush=True)
        report["browser_requests"] = check_browser_requests(page, [args.url, state["canonical"], args.url + "/all"])
        print("Requests through verified browser:", json.dumps(report["browser_requests"]), flush=True)
        return 0 if report["handoff_succeeded"] else 3
    except Exception as exc:  # noqa: BLE001 - always preserve diagnostic results from the probe
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(report["error"], flush=True)
        return 1
    finally:
        # Only non-secret diagnostics leave the dedicated browser profile.
        (run / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Report:", run / "report.json", flush=True)
        if page:
            page.socket.close()
        if browser:
            print("The separate browser window can be closed when you are done.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
