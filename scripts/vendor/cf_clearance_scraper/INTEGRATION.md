# Commander development probe

`main.py`, `requirements.txt`, `README.md`, and `LICENSE` are unmodified copies
from Xewdy444/CF-Clearance-Scraper. `UPSTREAM.json` records the exact revision and
SHA-256 of each copied file. Preserve the upstream MIT license.

The development entry point is `scripts/Test-ModDbClearance.py`. It calls the
upstream `CloudflareSolver` directly, including its challenge handling and
User-Agent metadata setup. The wrapper selects the installed browser, uses a
dedicated profile under ignored `build/`, disables browser archive downloads,
and tests the exported cookie with ordinary Windows curl. Both clients use a
direct connection. It does not use the CLI's curl impersonation profiles.

The wrapper passes the browser's actual User-Agent, an option supported by the
upstream implementation. It tests `cf_clearance` alone first and falls back to
all applicable ModDB cookies only if necessary. Secret headers reach curl over
stdin, not process arguments. Reports contain no cookie values or signed URLs.
It does not invoke the upstream command-line entry point, which logs cookies.

From the repository root, in PowerShell:

```powershell
.venv\Scripts\python.exe -m venv build\cf-clearance-venv
build\cf-clearance-venv\Scripts\python.exe -m pip install -r scripts\vendor\cf_clearance_scraper\requirements.txt
build\cf-clearance-venv\Scripts\python.exe scripts\Test-ModDbClearance.py
```

Use `--browser <exe>` to select a different Chromium browser and `--ids` for a
comma-separated list of numeric ModDB addon IDs. The default browser is Brave.

The live test on 2026-10-02 obtained clearance and reused that single cookie with
ordinary curl for addons 306772, 300660, and 246523. All six requests (each addon's
start and mirror-list pages) returned HTTP 200 without challenge HTML. This
verified metadata-page handoff; it did not download archives or test a complete
modpack update.

The production integration now lives in `commander_gui/moddb_session.py`.
`commander_gui/_vendor/cf_clearance_scraper.py` is an unchanged copy of this
solver. Each CLI worker starts an authenticated loopback bridge and gives its
endpoint/token to our CLI fork. The helper shares one clearance/actual-UA pair
across ModDB metadata and mirror requests, refreshing on a new challenge.
It uses a temporary dedicated browser profile, disables browser downloads,
and sends only the `cf_clearance` cookie to the exact ModDB origin. Redirects
are inspected without forwarding that cookie to download servers. The native
CLI continues to download archives and persist partial progress itself.

The production helper selects installed Brave, Chrome or Edge and uses a direct
connection for both browser and Windows curl. Verification has a 180-second
deadline, can be cancelled, and does not reopen repeatedly after failure.
Cookies and signed links are never written to helper logs. The portable spec
includes the helper dependencies and MIT license; no Python installation is
needed by portable users.
