"""Regression checks for frozen resource lookup and independent Assistant startup."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows package")


def test_frozen_cli_uses_bundle_resources(monkeypatch, tmp_path):
    from commander_gui.config import cli_binary_path, project_root

    resources = tmp_path / "São Paulo bundle" / "_internal"
    backend = resources / "cli/windows/stalker-gamma.exe"
    backend.parent.mkdir(parents=True)
    backend.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(resources), raising=False)
    monkeypatch.delenv("STALKER_GAMMA_CLI", raising=False)
    assert project_root() == resources
    assert cli_binary_path() == backend


def test_frozen_assistant_uses_same_exe_without_python_module(monkeypatch, tmp_path):
    from commander_gui.assistant_launcher import assistant_command

    executable = tmp_path / "Installed Commander" / "STALKER-GAMMA-COMMANDER.exe"
    archive = tmp_path / "São Paulo mod.zip"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert assistant_command(archive) == ([str(executable), "--assistant", str(archive)], executable.parent)


def test_frozen_assistant_resets_bootloader_environment(monkeypatch, tmp_path):
    from commander_gui import assistant_launcher

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Commander.exe"))
    monkeypatch.setattr(assistant_launcher, "_assistant_processes", [])
    popen = Mock()
    monkeypatch.setattr(assistant_launcher.subprocess, "Popen", popen)
    process = assistant_launcher.launch_assistant()
    assert process is popen.return_value
    assert popen.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert popen.call_args.kwargs["cwd"] == tmp_path
    assert assistant_launcher._assistant_processes == [process]


def test_windowless_assistant_qt_warning_uses_logging(monkeypatch):
    from assistant import __main__ as entry

    logger = Mock()
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(entry, "logging", SimpleNamespace(getLogger=lambda name: logger))
    entry.filtered_message_handler(None)(None, None, "test Qt warning")
    logger.warning.assert_called_once_with("Assistant Qt: %s", "test Qt warning")


def test_external_programs_do_not_inherit_bundled_qt_paths(monkeypatch, tmp_path):
    from commander_gui.windows import external_environment

    resources = tmp_path / "bundle/_internal"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(resources), raising=False)
    system = str(tmp_path / "System32")
    env = {
        "PATH": os.pathsep.join((str(resources / "PySide6"), system)),
        "QT_PLUGIN_PATH": str(resources / "PySide6/plugins"),
        "GAMMA_PROFILE": "Default",
    }
    assert external_environment(env) == {"PATH": system, "GAMMA_PROFILE": "Default"}
    assert "QT_PLUGIN_PATH" in env


def test_external_dll_directory_restored_on_launch_failure(monkeypatch):
    from commander_gui import windows

    def read_directory(size, buffer):
        buffer.value = r"C:\Commander\_internal"
        return len(buffer.value)

    kernel32 = SimpleNamespace(GetDllDirectoryW=Mock(side_effect=read_directory), SetDllDirectoryW=Mock(return_value=True))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(windows.ctypes, "WinDLL", Mock(return_value=kernel32))
    with pytest.raises(OSError, match="launch failure"), windows.external_dll_directory():
        raise OSError("launch failure")
    assert [call.args for call in kernel32.SetDllDirectoryW.call_args_list] == [(None,), (r"C:\Commander\_internal",)]
