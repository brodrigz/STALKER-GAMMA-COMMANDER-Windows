"""Run the vendored CF-Clearance-Scraper, then test its ordinary curl handoff.

Install scripts/vendor/cf_clearance_scraper/requirements.txt in an isolated venv.
Uses a dedicated browser profile. Cookies stay in memory / the browser profile;
curl receives headers over stdin, never through command-line arguments or logs.
No archives are downloaded and no game installation is modified.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "scripts/vendor/cf_clearance_scraper"


def load_upstream():
    spec = importlib.util.spec_from_file_location("cf_clearance_scraper", VENDOR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def curl_quote(value: str) -> str:
    if any(char in value for char in "\r\n\x00"):
        raise ValueError("Invalid curl config value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fetch(url: str, user_agent: str, cookies: list[dict]) -> dict:
    # Match upstream's generated command: ordinary curl + Cookie + User-Agent.
    # Restrict this diagnostic to a single origin and never forward cookies on redirects.
    if not re.fullmatch(r"https://www\.moddb\.com/addons/start/\d+(?:/all)?", url):
        raise ValueError("Unexpected probe URL")
    cookie_header = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
    config = "\n".join([
        "silent", "compressed", "include", "max-time = 30", "max-filesize = 2097152",
        'proxy = ""',  # Both the dedicated browser and curl use a direct connection.
        "url = " + curl_quote(url),
        "header = " + curl_quote("User-Agent: " + user_agent),
        "header = " + curl_quote("Cookie: " + cookie_header),
    ]) + "\n"
    result = subprocess.run(
        [shutil.which("curl.exe") or "curl", "--disable", "--config", "-"],
        input=config.encode(), capture_output=True, timeout=35, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    response = result.stdout.decode("utf-8", errors="replace")
    statuses = re.findall(r"(?m)^HTTP/[\d.]+ (\d{3})", response)
    status = int(statuses[-1]) if statuses else None
    challenge = "cf-mitigated: challenge" in response.lower() or "window._cf_chl_opt" in response
    return {"url": url, "status": status, "curl_exit_code": result.returncode,
            "challenge": challenge, "accepted": status == 200 and not challenge,
            "canonical_found": bool(re.search(r"rel=[\"']canonical[\"']", response)),
            "mirror_links_found": bool(re.search(r"id=[\"']downloadon[\"']", response)),
            "download_redirect_found": "window.location.href" in response,
            "response_bytes": len(result.stdout)}


async def run(args, directory: Path, report: dict) -> int:
    upstream = load_upstream()
    solver = upstream.CloudflareSolver(user_agent=None, timeout=args.timeout,
                                       http2=True, http3=True, headless=False, proxy=None)
    solver.driver.config.browser_executable_path = str(args.browser)
    solver.driver.config.user_data_dir = str(directory / "profile")
    solver.driver.config.add_argument("--no-proxy-server")
    async with solver:
        await solver.driver.main_tab.send(upstream.cdp.browser.set_download_behavior(behavior="deny"))
        # Use upstream's supported custom-UA path with this browser's actual UA.
        ua = await solver.get_user_agent()
        report["user_agent"] = ua
        url = f"https://www.moddb.com/addons/start/{args.ids[0]}"
        print("Running the copied upstream solver on " + url, flush=True)
        await asyncio.wait_for(solver.request_page(url), timeout=45)
        cookies = await solver.get_cookies()
        clearance = solver.extract_clearance_cookie(cookies)
        if clearance is None:
            await solver.set_user_agent_metadata(await solver.get_user_agent())
            # Navigation can return before the challenge script has initialized.
            for _ in range(10):
                if await solver.detect_challenge() is not None:
                    break
                await asyncio.sleep(0.5)
            print("Upstream challenge solver started in the visible browser.", flush=True)
            try:
                await asyncio.wait_for(solver.solve_challenge(), timeout=args.timeout)
            except asyncio.TimeoutError:
                report["solver_timeout"] = True
            cookies = await solver.get_cookies()
            clearance = solver.extract_clearance_cookie(cookies)
        report["clearance_obtained"] = clearance is not None
        if clearance is None:
            print("The upstream solver did not obtain a clearance cookie.", flush=True)
            return 2
        ua = await solver.get_user_agent()
        print("Clearance obtained. Testing ordinary curl with the exact browser User-Agent.", flush=True)
        applicable = [cookie for cookie in cookies
                      if cookie["domain"].lstrip(".").lower() in {"moddb.com", "www.moddb.com"}
                      and cookie.get("path", "/") == "/"]
        report["cookie_names"] = sorted({cookie["name"] for cookie in applicable})
        report["requests"] = []
        for cookie_mode, selected in (("clearance_only", [clearance]), ("all_cookies", applicable)):
            attempt = {"cookie_mode": cookie_mode, "results": []}
            report["requests"].append(attempt)
            for addon in args.ids:
                for suffix in ("", "/all"):
                    result = await asyncio.to_thread(fetch, f"https://www.moddb.com/addons/start/{addon}{suffix}", ua, selected)
                    attempt["results"].append(result)
                    print(json.dumps({"cookie_mode": cookie_mode, **result}), flush=True)
                    if not result["accepted"]:
                        break
                    await asyncio.sleep(1)
                if not result["accepted"]:
                    break
            if len(attempt["results"]) == len(args.ids) * 2 and all(item["accepted"] for item in attempt["results"]):
                report["handoff_succeeded"] = True
                return 0
        report["handoff_succeeded"] = False
        return 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, default=Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--ids", default="306772,300660,246523")
    args = parser.parse_args()
    args.ids = args.ids.split(",")
    if not all(re.fullmatch(r"\d+", addon) for addon in args.ids) or not 1 <= args.timeout <= 600:
        parser.error("Invalid addon IDs or timeout")
    if not args.browser.is_file():
        parser.error("Browser executable not found")
    # Upstream's CLI logs cookie values; invoke its class directly with logging disabled.
    logging.disable(logging.CRITICAL)
    directory = ROOT / "build/moddb-clearance-probe" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    report = {"upstream_revision": json.loads((VENDOR / "UPSTREAM.json").read_text())["revision"]}
    try:
        return asyncio.run(run(args, directory, report))
    except Exception as exc:  # noqa: BLE001 - preserve diagnostics without leaking request headers
        report["error_type"] = type(exc).__name__
        print("Probe failed: " + type(exc).__name__, flush=True)
        return 1
    finally:
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Report: " + str(directory / "report.json"), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
