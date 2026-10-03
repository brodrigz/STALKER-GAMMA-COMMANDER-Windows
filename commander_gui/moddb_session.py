"""One authenticated, loopback-only ModDB page session for a CLI invocation.

Cookies never leave this process (except the dedicated browser and curl stdin).
Only ModDB HTML and redirect headers cross the bridge; archives use the CLI's
normal resumable downloader. No redirects can forward clearance to the CDN.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .windows import external_dll_directory, external_environment

STATUS_PREFIX = "@commander-status "
ACCESS_PREFIX = "@commander-moddb "
MAX_PAGE_BYTES = 4 * 1024 * 1024


class ModDbSessionError(Exception):
    """Safe diagnostic: never include cookies, HTML or signed download URLs."""


def validate_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc.lower() != "www.moddb.com"
            or not parsed.path.startswith(("/addons/", "/downloads/", "/mods/"))
            or any(char in url for char in "\r\n\x00")):
        raise ModDbSessionError("Unsupported ModDB page address.")
    return url


def curl_quote(value: str) -> str:
    if any(char in value for char in "\r\n\x00"):
        raise ModDbSessionError("Invalid ModDB request header.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class Page:
    status: int
    headers: dict[str, str]
    body: str

    @property
    def challenged(self) -> bool:
        return (self.headers.get("cf-mitigated", "").lower() == "challenge"
                or "window._cf_chl_opt" in self.body)


def parse_response(raw: bytes) -> Page:
    # curl may include an interim HTTP/1.1 100 response before the final headers.
    while True:
        header, separator, body = raw.partition(b"\r\n\r\n")
        if not separator:
            raise ModDbSessionError("ModDB returned an incomplete HTTP response.")
        lines = header.decode("iso-8859-1").splitlines()
        match = re.fullmatch(r"HTTP/[\d.]+ (\d{3})(?: .*)?", lines[0])
        if not match:
            raise ModDbSessionError("ModDB returned an invalid HTTP response.")
        status = int(match[1])
        if 100 <= status < 200:
            raw = body
            continue
        headers = {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.lower()] = value.strip()
        return Page(status, headers, body.decode("utf-8", errors="replace"))


def find_browser() -> Path:
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            for relative in ("BraveSoftware/Brave-Browser/Application/brave.exe",
                             "Google/Chrome/Application/chrome.exe",
                             "Microsoft/Edge/Application/msedge.exe"):
                candidate = Path(base) / relative
                if candidate.is_file():
                    return candidate
    raise ModDbSessionError("ModDB verification needs Brave, Chrome or Edge installed.")


class ModDbSession:
    def __init__(self, notify=lambda message: None, access_changed=lambda state: None):
        self.notify = notify
        self.access_changed = access_changed
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._approval_lock = threading.Lock()
        self._approved = threading.Event()
        self._waiting = False
        self._generation = 0
        self._user_agent = ""
        self._clearance = ""
        self._expires = None

    def _status(self, state, message):
        self.access_changed({"state": state, "message": message, "expires": self._expires})

    def approve_verification(self) -> bool:
        """Called by the UI button; one click authorizes one browser attempt."""
        with self._approval_lock:
            if not self._waiting or self.stopped.is_set():
                return False
            self._waiting = False
            self._approved.set()
            return True

    def _wait_for_verification(self, url, message, state="required"):
        self._clearance = ""
        self._expires = None
        while True:
            with self._approval_lock:
                self._approved.clear()
                self._waiting = True
            self._status(state, message)
            while not self._approved.wait(0.2):
                if self.stopped.is_set():
                    raise ModDbSessionError("ModDB verification cancelled.")
            if self.stopped.is_set():
                raise ModDbSessionError("ModDB verification cancelled.")
            self._status("verifying", "Complete verification in the browser. Commander will close it and continue automatically.")
            try:
                asyncio.run(self._verify_until_stopped(url))
                self._generation += 1
                return
            except Exception as exc:  # noqa: BLE001 - browser diagnostics may contain cookies
                if self.stopped.is_set():
                    raise ModDbSessionError("ModDB verification cancelled.") from None
                message = (str(exc) if isinstance(exc, ModDbSessionError)
                           else "Browser verification failed. Click Verify in browser to try again.")
                state = "failed"

    def stop(self):
        self.stopped.set()

    def _fetch(self, url: str) -> Page:
        validate_url(url)
        curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/curl.exe"
        config = ["silent", "compressed", "include", "max-time = 35",
                  f"max-filesize = {MAX_PAGE_BYTES}", 'proxy = ""',
                  "url = " + curl_quote(url)]
        if self._user_agent:
            config.append("user-agent = " + curl_quote(self._user_agent))
        if self._clearance:
            config.append("header = " + curl_quote("Cookie: cf_clearance=" + self._clearance))
        # Do not enable --location: a signed CDN URL must never receive the cookie.
        with external_dll_directory():
            proc = subprocess.Popen(
                [str(curl), "--disable", "--config", "-"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=external_environment(dict(os.environ)),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        try:
            input_data = ("\n".join(config) + "\n").encode()
            while True:
                if self.stopped.is_set():
                    raise ModDbSessionError("ModDB request cancelled.")
                try:
                    output, _ = proc.communicate(input=input_data, timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    input_data = None
            if proc.returncode:
                raise ModDbSessionError(f"ModDB page request failed (curl code {proc.returncode}).")
            return parse_response(output)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    async def _verify(self, url: str):
        # This is the same upstream solver and ordinary-curl handoff verified by
        # Test-ModDbClearance.py. Never invoke its CLI: that prints cookie values.
        from ._vendor import cf_clearance_scraper as upstream

        for name in ("zendriver", "websockets"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        browser = find_browser()
        with tempfile.TemporaryDirectory(prefix="commander-moddb-", ignore_cleanup_errors=True) as profile:
            solver = upstream.CloudflareSolver(user_agent=None, timeout=150,
                                               http2=True, http3=True, headless=False, proxy=None)
            solver.driver.config.browser_executable_path = str(browser)
            solver.driver.config.user_data_dir = profile
            solver.driver.config.add_argument("--no-proxy-server")
            try:
                # Only browser process startup needs the frozen DLL path reset.
                with external_dll_directory():
                    await solver.driver.start()
                await solver.driver.main_tab.send(upstream.cdp.browser.set_download_behavior(behavior="deny"))
                await solver.request_page(url)
                await solver.set_user_agent_metadata(await solver.get_user_agent())
                for _ in range(20):
                    cookies = await solver.get_cookies()
                    if solver.extract_clearance_cookie(cookies) is not None:
                        break
                    if await solver.detect_challenge() is not None:
                        await solver.solve_challenge()
                        break
                    await asyncio.sleep(0.5)
                cookies = await solver.get_cookies()
                clearance = next((cookie for cookie in cookies
                                  if cookie["name"] == "cf_clearance"
                                  and cookie["domain"].lstrip(".").lower() in {"moddb.com", "www.moddb.com"}
                                  and cookie.get("path", "/") == "/"), None)
                if clearance is None:
                    raise ModDbSessionError("Verification was not completed. Click Verify in browser to try again.")
                self._user_agent = await solver.get_user_agent()
                self._clearance = clearance["value"]
                self._expires = clearance.get("expires")
            finally:
                # Zendriver can finish a listener with an exception as Chrome
                # disconnects. Await those tasks so shutdown diagnostics cannot
                # dump protocol content into the GUI log.
                connections = [solver.driver.connection, *solver.driver.targets]
                listeners = [connection.listener.task for connection in connections
                             if connection is not None and connection.listener is not None
                             and connection.listener.task is not None]
                try:
                    await asyncio.wait_for(solver.driver.stop(), timeout=10)
                except asyncio.TimeoutError:
                    # Only terminate the dedicated process we created, never a
                    # user's existing browser or profile.
                    process = solver.driver._process
                    if process is not None and process.poll() is None:
                        from .windows import terminate_process_tree

                        await asyncio.to_thread(terminate_process_tree, process.pid, process)
                finally:
                    for listener in listeners:
                        if not listener.done():
                            listener.cancel()
                    await asyncio.gather(*listeners, return_exceptions=True)

    async def _verify_until_stopped(self, url: str):
        task = asyncio.create_task(self._verify(url))
        try:
            deadline = asyncio.get_running_loop().time() + 180
            while not task.done():
                if self.stopped.is_set():
                    raise ModDbSessionError("ModDB verification cancelled.")
                if asyncio.get_running_loop().time() > deadline:
                    raise ModDbSessionError("Browser verification timed out. Click Verify in browser to try again.")
                await asyncio.sleep(0.2)
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def request(self, url: str, headers_only: bool = False, verification_required: bool = False) -> Page:
        validate_url(url)
        # Serialize short page requests and verification, never archive transfers.
        # Concurrent failures share one user-approved verification attempt.
        generation = self._generation
        with self._lock:
            if self.stopped.is_set():
                raise ModDbSessionError("ModDB request cancelled.")
            verified = False
            if verification_required and generation == self._generation:
                self._wait_for_verification(url, "Download returned HTTP 403. Cloudflare verification is required; saved download progress is kept.")
                verified = True
            redirects = 0
            while redirects < 6:
                page = self._fetch(url)
                if page.challenged or page.status == 403:
                    message = ("ModDB still rejects this session. Click Verify in browser to retry, or cancel and try later."
                               if verified else "ModDB requires Cloudflare verification (HTTP 403 / security challenge). Downloads waiting for links will continue afterward.")
                    self._wait_for_verification(url, message, "failed" if verified else "required")
                    verified = True
                    continue
                if 200 <= page.status < 400:
                    self._status("ready" if self._clearance else "not_needed",
                                 "Clearance cookie accepted by ModDB. Downloads continue automatically."
                                 if self._clearance else "ModDB is accessible. No clearance cookie is needed yet.")
                if headers_only and 300 <= page.status < 400 and page.headers.get("location"):
                    return Page(page.status, {"location": page.headers["location"]}, "")
                if not headers_only and page.status in (301, 302, 303, 307, 308):
                    url = validate_url(urljoin(url, page.headers.get("location", "")))
                    redirects += 1
                    continue
                if not 200 <= page.status < 300:
                    raise ModDbSessionError(f"ModDB returned HTTP {page.status}. Retry the update later.")
                return Page(page.status, {}, "" if headers_only else page.body)
            raise ModDbSessionError("ModDB returned too many redirects or verification attempts.")


class ModDbBridge:
    def __init__(self, notify=lambda message: None, session=None, access_changed=lambda state: None):
        self.session = session or ModDbSession(notify, access_changed)
        self.token = secrets.token_urlsafe(32)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log URLs, tokens or response bodies.

            def do_POST(self):
                if self.path != "/page" or not hmac.compare_digest(self.headers.get("X-Commander-Token", ""), bridge.token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8192:
                        raise ModDbSessionError("Invalid ModDB bridge request.")
                    request = json.loads(self.rfile.read(length))
                    if (not isinstance(request, dict) or not isinstance(request.get("url"), str)
                            or not isinstance(request.get("headersOnly", False), bool)
                            or not isinstance(request.get("verificationRequired", False), bool)):
                        raise ModDbSessionError("Invalid ModDB bridge request.")
                    page = bridge.session.request(request["url"], request.get("headersOnly", False),
                                                  request.get("verificationRequired", False))
                    payload = {"status": page.status, "headers": page.headers, "body": page.body}
                    status = 200
                except Exception as exc:  # noqa: BLE001 - never expose helper internals through the bridge
                    payload = {"error": str(exc) if isinstance(exc, ModDbSessionError) else "ModDB page request failed."}
                    status = 502
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def setup(self):
                super().setup()
                self.connection.settimeout(240)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)

    def start(self) -> dict[str, str]:
        self.thread.start()
        return {"COMMANDER_MODDB_BRIDGE": f"http://127.0.0.1:{self.server.server_port}/page",
                "COMMANDER_MODDB_TOKEN": self.token}

    def close(self):
        self.session.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
