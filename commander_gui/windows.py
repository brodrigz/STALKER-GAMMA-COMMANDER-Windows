"""Native Windows process discovery and command-line handling."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

import psutil

_DLL_DIRECTORY_LOCK = threading.RLock()


def external_environment(env: dict[str, str]) -> dict[str, str]:
    """Keep a frozen application's Qt/Python paths out of external programs."""
    clean = env.copy()
    if not getattr(sys, "frozen", False) or not hasattr(sys, "_MEIPASS"):
        return clean
    resources = Path(sys._MEIPASS).resolve()

    def bundled(value: str) -> bool:
        return bool(value) and Path(value.strip('"')).resolve().is_relative_to(resources)

    clean["PATH"] = os.pathsep.join(value for value in clean.get("PATH", "").split(os.pathsep) if not bundled(value))
    for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH", "QML_IMPORT_PATH"):
        if key in clean:
            clean[key] = os.pathsep.join(value for value in clean[key].split(os.pathsep) if not bundled(value))
            if not clean[key]:
                clean.pop(key)
    return clean


@contextmanager
def external_dll_directory():
    """Do not let MO2 or the game inherit PyInstaller's DLL search directory."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        yield
        return
    with _DLL_DIRECTORY_LOCK:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_directory = kernel32.GetDllDirectoryW
        get_directory.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
        get_directory.restype = wintypes.DWORD
        set_directory = kernel32.SetDllDirectoryW
        set_directory.argtypes = [wintypes.LPCWSTR]
        set_directory.restype = wintypes.BOOL
        previous = ctypes.create_unicode_buffer(32768)
        get_directory(len(previous), previous)
        if not set_directory(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not set_directory(previous.value or None):
                raise ctypes.WinError(ctypes.get_last_error())


def executable_pids(name: str) -> set[int]:
    """Match executable names, never substrings of another process's arguments.

    Guards deliberately include all MO2 instances: an inaccessible process
    path must not let Commander overwrite a modlist MO2 may still own.
    """
    wanted = name.casefold()
    return {
        process.info["pid"]
        for process in psutil.process_iter(["pid", "name"])
        if (process.info["name"] or "").casefold() == wanted
    }


def game_running() -> bool:
    for process in psutil.process_iter(["name"]):
        name = process.info["name"] or ""
        if name.casefold() == "modorganizer.exe" or re.fullmatch(
            r"Anomaly[A-Za-z0-9]*\.exe", name, re.IGNORECASE
        ):
            return True
    return False


def split_arguments(arguments: str) -> list[str]:
    """Parse Windows quoting while preserving backslashes and empty arguments."""
    if not arguments.strip():
        return []
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    parse = shell32.CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    free = kernel32.LocalFree
    free.argtypes = [wintypes.HLOCAL]
    free.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    # argv[0] has special parsing rules; prepend a dummy executable so all
    # of the supplied string is interpreted as arguments.
    argv = parse("commander.exe " + arguments, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] for index in range(1, count.value)]
    finally:
        free(ctypes.cast(argv, wintypes.HLOCAL))


def format_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


class PausedProcessTree:
    """Suspend only an owned worker tree; retain process identities for resume."""

    def __init__(self):
        self._processes: list[psutil.Process] = []

    def suspend(self, worker: subprocess.Popen) -> bool:
        if self._processes:
            return True
        if worker.poll() is not None:
            return False
        try:
            root = psutil.Process(worker.pid)
            root.suspend()
            self._processes.append(root)
            # Freeze each generation before discovering the next, preventing
            # an extraction helper from spawning more children during traversal.
            pending = [root]
            seen = {root.pid}
            while pending:
                parent = pending.pop()
                for child in parent.children():
                    if child.pid in seen:
                        continue
                    seen.add(child.pid)
                    try:
                        child.suspend()
                    except psutil.NoSuchProcess:
                        continue
                    self._processes.append(child)
                    pending.append(child)
            return True
        except psutil.NoSuchProcess:
            self.resume()
            return False
        except psutil.Error as exc:
            self.resume()
            raise OSError(f"Could not pause the installer: {exc}") from exc

    def resume(self) -> bool:
        # Resume descendants first. psutil guards against PID reuse.
        while self._processes:
            process = self._processes[-1]
            try:
                process.resume()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error as exc:
                raise OSError(f"Could not resume the installer: {exc}") from exc
            self._processes.pop()
        return True


def terminate_process_tree(pid: int, process: subprocess.Popen | None = None) -> None:
    """Stop a worker and its helpers without taskkill's WMI dependency."""
    if process is not None and process.poll() is not None:
        return
    try:
        parent = psutil.Process(pid)
        # Prevent the worker spawning another helper during enumeration.
        parent.suspend()
    except psutil.NoSuchProcess:
        return
    except psutil.Error as exc:
        raise OSError(str(exc)) from exc
    try:
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
        psutil.wait_procs([*children, parent], timeout=3)
    except psutil.NoSuchProcess:
        pass
    except psutil.Error as exc:
        raise OSError(str(exc)) from exc
    finally:
        # An access error must not leave an otherwise live worker suspended.
        try:
            parent.resume()
        except psutil.Error:
            pass
    if process is not None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
