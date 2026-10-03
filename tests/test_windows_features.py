import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest
from PySide6.QtWidgets import QApplication

from commander_gui import windows_runtimes as runtimes
from commander_gui.windows import PausedProcessTree, terminate_process_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows features")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_runtime_checks_both_architectures_and_missing_files(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(runtimes, "_vc_version", lambda arch: (14, 51, 36247, 0))
    for directory in ("System32", "SysWOW64"):
        target = tmp_path / directory
        target.mkdir()
        for filename in (*runtimes.VC_DLLS, *runtimes.DX_DLLS):
            (target / filename).touch()
    assert runtimes.runtime_summary(runtimes.check_runtimes())[0] is True
    (tmp_path / "SysWOW64/d3dx9_43.dll").unlink()
    results = {item.key: item for item in runtimes.check_runtimes()}
    assert results["dx_x64"].installed
    assert not results["dx_x86"].installed
    assert "d3dx9_43.dll" in results["dx_x86"].detail
    monkeypatch.setattr(runtimes, "_vc_version", lambda arch: (14, 29) if arch == "x86" else (14, 51))
    assert not next(c for c in runtimes.check_runtimes() if c.key == "vc_x86").installed


def test_unreadable_registry_is_unknown_not_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(runtimes, "_vc_version", Mock(side_effect=PermissionError("Access denied")))
    assert next(c for c in runtimes.check_runtimes() if c.key == "vc_x64").installed is None


@pytest.mark.parametrize("status,publisher,accepted", [
    ("Valid", "Microsoft Corporation", True),
    ("NotSigned", "Microsoft Corporation", False),
    ("HashMismatch", "Microsoft Corporation", False),
    ("Valid", "Another Publisher", False),
])
def test_signature_required_before_install(monkeypatch, tmp_path, status, publisher, accepted):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"status": status, "publisher": publisher})))
    monkeypatch.setattr(runtimes.subprocess, "run", run)
    installer = tmp_path / "file with spaces.exe"
    if accepted:
        runtimes.verify_microsoft_signature(installer)
    else:
        with pytest.raises(OSError, match="signature"):
            runtimes.verify_microsoft_signature(installer)
    assert str(installer) not in " ".join(run.call_args.args[0])
    assert run.call_args.kwargs["env"]["COMMANDER_RUNTIME_INSTALLER"] == str(installer)


def setup_fake_install(monkeypatch, exit_code=0):
    checks = [runtimes.RuntimeCheck("vc_x64", "VC x64", False, "missing"),
              runtimes.RuntimeCheck("vc_x86", "VC x86", True, "installed")]
    monkeypatch.setattr(runtimes, "check_runtimes", lambda: checks)
    monkeypatch.setattr(runtimes, "game_running", lambda: False)
    monkeypatch.setattr(runtimes, "download_installer", Mock())
    monkeypatch.setattr(runtimes, "verify_microsoft_signature", Mock())
    run = Mock(return_value=exit_code)
    monkeypatch.setattr(runtimes, "run_elevated", run)
    return run


def test_only_missing_runtime_installed_and_reboot_reported(monkeypatch):
    run = setup_fake_install(monkeypatch, 3010)
    result = runtimes.install_missing_runtimes(lambda _: None, threading.Event())
    assert result["reboot"]
    run.assert_called_once()
    assert run.call_args.args[0].name == "vc_redist.x64.exe"
    assert "/norestart" in run.call_args.args[1]
    assert result["checks"][0].installed is False  # don't fabricate post-install success


def test_failed_signature_prevents_elevation(monkeypatch):
    run = setup_fake_install(monkeypatch)
    monkeypatch.setattr(runtimes, "verify_microsoft_signature", Mock(side_effect=OSError("bad signature")))
    with pytest.raises(OSError, match="bad signature"):
        runtimes.install_missing_runtimes(lambda _: None, threading.Event())
    run.assert_not_called()


def test_stop_before_setup_launches_nothing(monkeypatch):
    run = setup_fake_install(monkeypatch)
    cancel = threading.Event()
    cancel.set()
    result = runtimes.install_missing_runtimes(lambda _: None, cancel)
    assert result["cancelled"]
    run.assert_not_called()


def test_uac_declined_is_cancellation(monkeypatch):
    run = setup_fake_install(monkeypatch)
    run.side_effect = runtimes.SetupCancelled("Windows elevation was cancelled.")
    result = runtimes.install_missing_runtimes(lambda _: None, threading.Event())
    assert result["cancelled"]


def test_game_started_during_download_prevents_installer(monkeypatch):
    run = setup_fake_install(monkeypatch)
    monkeypatch.setattr(runtimes, "game_running", Mock(side_effect=[False, True]))
    with pytest.raises(OSError, match="Close Mod Organizer"):
        runtimes.install_missing_runtimes(lambda _: None, threading.Event())
    run.assert_not_called()


def test_windows_notification_delivery_uses_qt_queue(app, monkeypatch):
    from commander_gui import notifications

    tray = Mock()
    factory = Mock(return_value=tray)
    factory.isSystemTrayAvailable.return_value = True
    factory.supportsMessages.return_value = True
    factory.MessageIcon.Information = 1
    monkeypatch.setattr(notifications, "QSystemTrayIcon", factory)
    service = notifications.DesktopNotifications(app)
    thread = threading.Thread(target=lambda: service.requested.emit("Download finished", "All addons installed"))
    thread.start()
    thread.join()
    tray.showMessage.assert_not_called()
    app.processEvents()
    tray.showMessage.assert_called_once_with("Download finished", "All addons installed", 1, 10000)
    service.deleteLater()


def test_pause_resume_real_owned_process_and_child(tmp_path):
    # Only harmless test processes are suspended, never the user's running CLI.
    child_code = "import time; time.sleep(60)"
    code = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    child_pid_path = tmp_path / "child.txt"
    worker = subprocess.Popen([sys.executable, "-c", code, str(child_pid_path), child_code], creationflags=subprocess.CREATE_NO_WINDOW)
    paused = PausedProcessTree()
    child = None
    try:
        for _ in range(100):
            if child_pid_path.exists() and child_pid_path.read_text():
                break
            time.sleep(0.02)
        child = psutil.Process(int(child_pid_path.read_text()))
        assert paused.suspend(worker)
        assert psutil.Process(worker.pid).status() == psutil.STATUS_STOPPED
        assert child.status() == psutil.STATUS_STOPPED
        assert paused.resume()
        assert psutil.Process(worker.pid).status() != psutil.STATUS_STOPPED
        assert child.status() != psutil.STATUS_STOPPED
        assert paused.suspend(worker)
        terminate_process_tree(worker.pid, worker)
        assert worker.poll() is not None
        assert not child.is_running()
    finally:
        paused.resume()
        if worker.poll() is None:
            terminate_process_tree(worker.pid, worker)
        if child is not None and child.is_running():
            child.kill()


def test_partial_pause_failure_rolls_back(monkeypatch):
    root = Mock()
    root.pid = 1
    child = Mock()
    child.pid = 2
    root.children.return_value = [child]
    child.suspend.side_effect = psutil.AccessDenied(2)
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    worker = SimpleNamespace(pid=1, poll=lambda: None)
    with pytest.raises(OSError):
        PausedProcessTree().suspend(worker)
    root.resume.assert_called_once()


def test_download_pause_ui_keeps_bytes_and_clears_speed(app):
    from commander_gui.parsers import ProgressEvent
    from commander_gui.ui.common import ProgressArea

    area = ProgressArea()
    runner = Mock()
    runner.pause.return_value = True
    runner.resume.return_value = True
    area.set_runner(runner)
    area.table.upsert(ProgressEvent(name="Addon", operation="Download", percent=0.4, complete=0, total=1,
                                    bytes_downloaded=40, total_bytes=100, bytes_per_second=10, structured=True))
    area._toggle_pause()
    assert area.is_paused
    assert area.table.item(0, 1).text() == "Paused"
    assert area.table.cellWidget(0, 2).value() == 400
    assert area.table.item(0, 3).text() == "—"
    area.table._expire_speed()
    assert area.table.item(0, 1).text() == "Paused"
    area._toggle_pause()
    assert not area.is_paused
    assert area.table.cellWidget(0, 2).value() == 400
    area.table.reset()
    area.deleteLater()


def test_failed_pause_does_not_lie_in_ui(app):
    from commander_gui.ui.common import ProgressArea

    area = ProgressArea()
    runner = Mock()
    runner.pause.return_value = False
    area.set_runner(runner)
    area._toggle_pause()
    assert not area.is_paused
    area.deleteLater()


def test_runtime_recheck_button_and_failed_setup_release_busy(app, monkeypatch):
    from commander_gui.ui import runtime_setup

    checks = [runtimes.RuntimeCheck("vc_x64", "VC", False, "missing")]
    monkeypatch.setattr(runtime_setup, "check_runtimes", lambda: checks)
    window = Mock(install_busy=False)
    dialog = runtime_setup.RuntimeSetupDialog(window)
    dialog.refresh_button.click()  # clicked(bool) must not be interpreted as a list of checks
    assert dialog.table.rowCount() == 1
    assert dialog.install_button.isEnabled()
    dialog._running = True
    dialog._error("fixture download failure")
    window.set_install_busy.assert_called_with(False)
    assert not dialog._running
    assert "fixture download failure" in dialog.status.text()
    dialog.deleteLater()


def test_cloudflare_status_visible_with_console_hidden(app):
    from commander_gui.moddb_session import ACCESS_PREFIX
    from commander_gui.ui.common import ProgressArea

    area = ProgressArea(auto_expand_log=False)
    runner = Mock()
    runner.verify_moddb.return_value = True
    area.set_runner(runner)
    area.on_started()
    area.on_line(ACCESS_PREFIX + json.dumps({"state": "required", "message": "HTTP 403: verify to continue."}))
    assert area.log.isHidden()
    assert not area.moddb_access.isHidden()
    assert "Verification required" in area.moddb_access.status.text()
    assert area.moddb_access.button.isEnabled()
    runner.verify_moddb.assert_not_called()
    area.moddb_access.button.click()
    runner.verify_moddb.assert_called_once()
    assert not area.moddb_access.button.isEnabled()
    area.on_line(ACCESS_PREFIX + json.dumps({"state": "ready", "message": "Cookie accepted"}))
    assert "Cookie accepted" in area.moddb_access.status.text()
    area.on_finished(1, "cancelled")
    area.on_line(ACCESS_PREFIX + json.dumps({"state": "required", "message": "late event"}))
    assert not area.moddb_access.button.isEnabled()
    assert "Session ended" in area.moddb_access.status.text()
    area.deleteLater()
