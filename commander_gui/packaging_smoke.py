"""Offline checks of the actual frozen application, used by the build script."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path
from unittest.mock import patch


def run(args: list[str]) -> int:
    report_path = Path(args[args.index("--packaging-smoke-test") + 1]).resolve()
    assistant = "--assistant" in args
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    checks: list[str] = []
    report = {"component": "assistant" if assistant else "commander", "checks": checks}
    result = 1
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtSvg import QSvgRenderer
        from PySide6.QtWidgets import QApplication

        from .config import cli_binary_path, project_root

        assert getattr(sys, "frozen", False), "Smoke test must run from the packaged executable"
        root = project_root()
        assert root.is_dir()
        report["resources"] = str(root)
        report["executable"] = sys.executable
        for relative in (
            "commander_gui/assets/pda.wav", "commander_gui/assets/discord.svg",
            "commander_gui/fonts/Exo2-Variable.ttf", "assistant/fonts/Exo2-Variable.ttf",
            "cli/stalker-gamma.png", "cli/windows-backend.json",
            "cli/windows/stalker-gamma.exe", "cli/windows/resources/7zz.exe",
            "cli/windows/resources/7z.dll", "cli/windows/resources/cloudscraper.exe",
            "cli/windows/cacert.pem", "cli/windows/libcurl-impersonate.dll",
        ):
            assert (root / relative).is_file(), f"Missing packaged resource: {relative}"
        checks.append("resources")

        if assistant:
            from assistant import __main__ as entry
            from assistant.ui.main_window import MainWindow
        else:
            from . import main as entry
            from .ui.main_window import NAV_ITEMS, MainWindow

        failures: list[str] = []

        class SmokeApplication(QApplication):
            def __init__(self, argv):
                super().__init__(argv)
                QTimer.singleShot(200, self.check_window)

            def check_window(self):
                try:
                    window = next(w for w in self.topLevelWidgets() if isinstance(w, MainWindow))
                    if not assistant:
                        for name, _ in NAV_ITEMS:
                            window._ensure_page(name)
                        window._ensure_page("settings")
                        from .ui.runtime_setup import RuntimeSetupDialog

                        dialog = RuntimeSetupDialog(window)
                        dialog.refresh_button.click()
                        assert dialog.table.rowCount() == 4
                        dialog.deleteLater()
                        assert self._desktop_notifications is not None
                        checks.append("Windows runtime checks, setup dialog and notification service")
                    assert "Exo 2" in QFontDatabase.families(), "Bundled font was not loaded"
                    assert QSvgRenderer(str(root / "commander_gui/assets/discord.svg")).isValid()
                    checks.append("Qt windows, fonts, SVG and page imports")
                except Exception:  # noqa: BLE001 - record any startup failure in the smoke report
                    failures.append(traceback.format_exc())
                finally:
                    self.exit(1 if failures else 0)

        import user_agents

        from ._vendor.cf_clearance_scraper import CloudflareSolver
        from .moddb_session import ModDbBridge
        from .ui.common import BackgroundTask

        assert CloudflareSolver is not None
        assert user_agents.parse("Mozilla/5.0 Chrome/154.0.0.0 Safari/537.36").browser.family == "Chrome"
        bridge = ModDbBridge()
        try:
            assert bridge.start()["COMMANDER_MODDB_BRIDGE"].startswith("http://127.0.0.1:")
        finally:
            bridge.close()
        checks.append("browser helper dependencies and local ModDB bridge")

        # Exercise Qt's real event loop and all pages without contacting update
        # servers or querying any existing user's MO2 installation.
        with (
            patch.object(entry, "QApplication", SmokeApplication),
            patch.object(BackgroundTask, "start", lambda self: None),
            patch("urllib.request.urlopen", side_effect=OSError("offline packaging test")),
        ):
            if assistant:
                with patch.object(sys, "argv", [sys.executable]):
                    try:
                        entry.main()
                    except SystemExit as exc:
                        result = int(exc.code or 0)
            else:
                result = entry.main([sys.executable])
        assert result == 0 and not failures, "\n".join(failures)

        from .assistant_launcher import assistant_command
        from .cli_runner import run_sync
        from .mod_install import extract_archive

        command, cwd = assistant_command()
        assert command == [sys.executable, "--assistant"]
        assert cwd == Path(sys.executable).parent
        checks.append("frozen Assistant dispatch")
        if not assistant:
            from .assistant_launcher import launch_assistant, reap_assistant_processes

            with tempfile.TemporaryDirectory(prefix="commander-assistant-") as temporary:
                child_report = Path(temporary) / "assistant.json"
                child_command = [sys.executable, "--assistant", "--packaging-smoke-test", str(child_report)]
                with patch("commander_gui.assistant_launcher.assistant_command", return_value=(child_command, cwd)):
                    child = launch_assistant()
                try:
                    assert child.wait(timeout=20) == 0, "Frozen Assistant child failed"
                    assert json.loads(child_report.read_text(encoding="utf-8"))["ok"]
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    reap_assistant_processes()
            checks.append("independent Assistant subprocess")

        from .windows import external_dll_directory, external_environment

        kernel32 = ctypes.WinDLL("kernel32")

        def dll_directory():
            buffer = ctypes.create_unicode_buffer(32768)
            kernel32.GetDllDirectoryW(len(buffer), buffer)
            return buffer.value

        previous_directory = dll_directory()
        with external_dll_directory():
            assert not dll_directory(), "External apps would inherit the bundled DLL directory"
        assert dll_directory() == previous_directory, "Commander DLL search directory was not restored"
        env = external_environment({"PATH": str(root / "PySide6"), "QT_PLUGIN_PATH": str(root / "PySide6/plugins")})
        assert not env["PATH"] and "QT_PLUGIN_PATH" not in env
        checks.append("external application library isolation")
        rc, output = run_sync(["--help"], timeout=15)
        assert rc == 0 and "full-install" in output, output
        assert cli_binary_path().is_relative_to(root)
        checks.append("native backend subprocess")
        with tempfile.TemporaryDirectory(prefix="commander-package-") as temporary:
            archive = Path(temporary) / "mod archive.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("gamedata/configs/test.ltx", "[package]\nvalid = true\n")
            staging = Path(temporary) / "unpacked mod"
            extract_archive(archive, staging)
            assert (staging / "gamedata/configs/test.ltx").read_text() == "[package]\nvalid = true\n"
        checks.append("native archive extraction")
        report["ok"] = True
        result = 0
    except Exception:  # noqa: BLE001 - preserve diagnostics from a windowless executable
        report["ok"] = False
        report["error"] = traceback.format_exc()
        result = 1
    finally:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return result
