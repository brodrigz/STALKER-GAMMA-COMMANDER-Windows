"""Native Windows backend, process and UI regression coverage."""

import os
import subprocess
import sys
import threading
import time
import zipfile
from types import SimpleNamespace

import psutil
import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows integration")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_paths_match_native_cli(monkeypatch, tmp_path):
    from commander_gui.config import cli_binary_path, settings_dir
    from commander_gui.mod_install import find_archiver

    monkeypatch.delenv("STALKER_GAMMA_CLI", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert settings_dir() == tmp_path / "Roaming" / "stalker-gamma"
    assert cli_binary_path().name == "stalker-gamma.exe"
    backend = tmp_path / "custom CLI" / "stalker-gamma.exe"
    archiver = backend.parent / "resources" / "7zz.exe"
    archiver.parent.mkdir(parents=True)
    archiver.touch()
    monkeypatch.setenv("STALKER_GAMMA_CLI", str(backend))
    assert find_archiver() == archiver


def test_native_cli_preserves_options_but_omits_unsupported_interval():
    from commander_gui.cli_runner import cli_command

    options = ["full-install", "--minimal", "--preserve-user-settings", "--preserve-mcm-settings"]
    assert cli_command(options, progress_interval_ms=200)[1:] == options


@pytest.mark.parametrize("args", [
    [r"C:\GAMMA Mods\user.ltx", "", "-dbg"],
    ["a\"b", "C:\\Game folder\\", "São Paulo", "100%", "&"],
])
def test_native_argument_round_trip(args):
    from commander_gui.windows import format_command, split_arguments

    assert split_arguments(format_command(args)) == args


def test_windows_z_drive_is_not_a_wine_mapping():
    from commander_gui.launcher import mo2_path_to_host

    assert mo2_path_to_host(r"Z:\GAMMA\Anomaly.exe") == r"Z:\GAMMA\Anomaly.exe"


def test_real_windows_archiver_extracts_local_mod(tmp_path):
    from commander_gui.config import cli_binary_path
    from commander_gui.mod_install import extract_archive

    if not (cli_binary_path().parent / "resources" / "7zz.exe").is_file():
        pytest.skip("Windows backend is not installed")
    archive = tmp_path / "local mod.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("gamedata/configs/test.ltx", "[test]\nvalue = 1\n")
    staging = tmp_path / "extracted mod"
    extract_archive(archive, staging)
    assert (staging / "gamedata/configs/test.ltx").read_text() == "[test]\nvalue = 1\n"


def test_native_playtime_stops_when_game_exits_before_mo2(monkeypatch, tmp_path):
    from commander_gui.ui import play_page

    recorded = []
    running = {123}
    monkeypatch.setattr(play_page, "exe_pids", lambda name: running.copy())
    page = SimpleNamespace(
        _game_exe_name="AnomalyDX11.exe", _pre_launch_game_pids=set(),
        _game_seen=False, _record_playtime=lambda: recorded.append(True),
        _check_for_crash=lambda: None, _retry_discord_presence=lambda: None,
        _proc=SimpleNamespace(poll=lambda: None),
    )
    play_page.PlayPage._on_launch_check(page, "GAMMA", [], tmp_path / "launcher.log")
    assert page._game_seen
    running.clear()
    play_page.PlayPage._on_launch_check(page, "GAMMA", [], tmp_path / "launcher.log")
    assert recorded == [True]
    assert page._game_exe_name is None


def test_direct_launch_keeps_windows_paths():
    from commander_gui.launcher import (
        Mo2Executable,
        build_direct_command,
        resolve_runner,
    )

    exe = Mo2Executable(
        binary=r"C:\Games\Anomaly\bin\AnomalyDX11.exe",
        working_directory=r"C:\Games\Anomaly",
        arguments='-file "C:\\Mod settings\\user.ltx"',
    )
    command, env, cwd = build_direct_command(exe, resolve_runner("auto"))
    assert command == [exe.binary, "-file", r"C:\Mod settings\user.ltx"]
    assert env == {}
    assert cwd == exe.working_directory


def test_process_guards_use_executable_names(monkeypatch):
    from commander_gui import windows
    from commander_gui.ui import common

    processes = [
        SimpleNamespace(info={"pid": 1, "name": "modorganizer.EXE"}),
        SimpleNamespace(info={"pid": 2, "name": "AnomalyDX11AVX.exe"}),
        SimpleNamespace(info={"pid": 3, "name": "ModOrganizer.exe.backup"}),
    ]
    monkeypatch.setattr(windows.psutil, "process_iter", lambda attrs: iter(processes))
    assert common.mo2_running(force=True)
    assert common.mo2_pids() == {1}
    assert common.exe_pids("AnomalyDX11AVX.exe") == {2}
    assert common.game_running(force=True)
    processes[:] = processes[2:]
    assert not common.mo2_running(force=True)
    assert not common.game_running(force=True)


def test_atomic_write_recovers_after_sharing_failure(monkeypatch, tmp_path):
    from commander_gui import atomic

    target = tmp_path / "modlist.txt"
    atomic.write_text(target, "+original\n")
    replace = atomic.os.replace
    with monkeypatch.context() as patcher:
        def denied(*args):
            raise PermissionError("File is open in MO2")
        patcher.setattr(atomic.os, "replace", denied)
        with pytest.raises(PermissionError):
            atomic.write_text(target, "+changed\n")
    assert target.read_text() == "+original\n"
    assert not list(tmp_path.glob("*.tmp"))
    assert atomic.os.replace is replace
    atomic.write_text(target, "+changed\n")
    assert target.read_text() == "+changed\n"


def test_cancel_stops_installer_and_helper(tmp_path):
    from commander_gui.cli_runner import CliWorker

    pid_file = tmp_path / "helper.pid"
    script = tmp_path / "worker.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(60)\n", encoding="utf-8",
    )
    worker = CliWorker()
    worker.setup([sys.executable, str(script), str(pid_file)])
    thread = threading.Thread(target=worker.run)
    thread.start()
    child = None
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "Worker did not spawn its helper"
        child = psutil.Process(int(pid_file.read_text()))
        worker.cancel()
        thread.join(timeout=10)
        assert not thread.is_alive()
        child.wait(timeout=5)
    finally:
        worker.kill()
        if child is not None and child.is_running():
            child.kill()
        thread.join(timeout=5)


def test_cli_help_contract():
    from commander_gui.config import cli_binary_path

    binary = cli_binary_path()
    if not binary.is_file():
        pytest.skip("Run scripts/Setup-Windows.ps1 to install the integration backend")
    result = subprocess.run([str(binary), "--help"], capture_output=True, text=True, timeout=15, check=True)
    assert result.returncode == 0
    for command in ("anomaly check", "anomaly install", "cache prune apply", "gog fix-install", "update apply"):
        assert command in result.stdout
    result = subprocess.run([str(binary), "full-install", "--help"], capture_output=True, text=True, timeout=15, check=True)
    for option in ("--minimal", "--preserve-user-settings", "--preserve-mcm-settings", "--skip-extract-on-hash-match"):
        assert option in result.stdout
    assert "--progress-update-interval-ms" not in result.stdout


def test_all_desktop_pages_construct_on_windows(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from commander_gui import gui_settings
    from commander_gui.ui.common import BackgroundTask
    from commander_gui.ui.main_window import NAV_ITEMS, MainWindow

    app = QApplication.instance() or QApplication([])
    # This smoke test constructs real widgets; backend jobs are covered
    # separately and must not query the user's installed CLI profiles here.
    monkeypatch.setattr(BackgroundTask, "start", lambda self: None)
    gui_settings.save_gui_settings(welcome_hidden=True)
    window = MainWindow()
    try:
        for name, _ in NAV_ITEMS:
            window._ensure_page(name)
        window._ensure_page("settings")
        play = window._pages["play"]
        assert play.runner_combo.currentData() == "native"
        assert play._releases == []
        assert window._pages["install"].winetricks_button.isEnabled()
        assert window._pages["install"].winetricks_button.text() == "Set up Windows runtimes"
        assert list(window._pages["systemcheck"]._sections) == ["Windows"]
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents()


def test_native_entry_point_runs_event_loop(tmp_path):
    """Exercise real startup/shutdown in its own Qt process, without network."""
    code = '''
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from commander_gui import main

class ShortSession(QApplication):
    def __init__(self, args):
        super().__init__(args)
        QTimer.singleShot(500, self.quit)

main.QApplication = ShortSession
with patch("urllib.request.urlopen", side_effect=OSError("offline startup test")):
    raise SystemExit(main.main(["commander"]))
'''
    env = dict(os.environ, APPDATA=str(tmp_path / "roaming"), LOCALAPPDATA=str(tmp_path / "local"))
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True,
        text=True, timeout=25, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
