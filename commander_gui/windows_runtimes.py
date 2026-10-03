"""Detect Windows game prerequisites and run verified Microsoft installers."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import subprocess
import tempfile
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .windows import external_dll_directory, external_environment, game_running

VC_URL = "https://aka.ms/vc14/vc_redist.{arch}.exe"
DX_URL = "https://download.microsoft.com/download/8/4/a/84a35bf1-dafe-4ae8-82af-ad2ae20b6b14/directx_Jun2010_redist.exe"
DX_DLLS = ("d3dx9_43.dll", "d3dx10_43.dll", "d3dx11_43.dll", "d3dcompiler_43.dll", "xinput1_3.dll", "xaudio2_7.dll")
VC_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")


@dataclass(frozen=True)
class RuntimeCheck:
    key: str
    name: str
    installed: bool | None
    detail: str


class SetupCancelled(Exception):
    pass


def _vc_version(arch: str) -> tuple[int, ...] | None:
    import winreg

    versions = []
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                rf"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\{arch}",
                                0, winreg.KEY_READ | view) as key:
                if winreg.QueryValueEx(key, "Installed")[0] != 1:
                    continue
                value = winreg.QueryValueEx(key, "Version")[0]
                parts = tuple(int(n) for n in re.findall(r"\d+", str(value)))
                if parts:
                    versions.append(parts)
        except FileNotFoundError:
            continue
    return max(versions) if versions else None


def check_runtimes() -> list[RuntimeCheck]:
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    checks = []
    for arch, folder in (("x64", "System32"), ("x86", "SysWOW64")):
        directory = root / folder
        # Windows x86's vcruntime140_1 implementation is part of vcruntime140.
        dlls = VC_DLLS if arch == "x64" else tuple(name for name in VC_DLLS if name != "vcruntime140_1.dll")
        try:
            version = _vc_version(arch)
            missing = [name for name in dlls if not (directory / name).is_file()]
            ready = version is not None and version >= (14, 30) and not missing
            detail = "Version " + ".".join(map(str, version)) if version else "Not registered"
            if version and version < (14, 30):
                detail += " — update required for Visual C++ 2022 applications"
            if missing:
                detail += "; missing " + ", ".join(missing)
            checks.append(RuntimeCheck("vc_" + arch, f"Visual C++ v14 ({arch})", ready, detail))
        except OSError as exc:
            checks.append(RuntimeCheck("vc_" + arch, f"Visual C++ v14 ({arch})", None, f"Could not verify: {exc}"))
        missing = [name for name in DX_DLLS if not (directory / name).is_file()]
        checks.append(RuntimeCheck("dx_" + arch, f"DirectX June 2010 ({arch})", not missing,
                                   "Required libraries present" if not missing else "Missing " + ", ".join(missing)))
    return checks


def runtime_summary(checks: list[RuntimeCheck]) -> tuple[bool | None, str]:
    ready = sum(check.installed is True for check in checks)
    state = True if checks and ready == len(checks) else (None if any(c.installed is None for c in checks) else False)
    return state, f"{ready}/{len(checks)} runtime checks passed"


class _HttpsRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise OSError("The runtime download attempted an insecure redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _cancelled(cancel: threading.Event):
    if cancel.is_set():
        raise SetupCancelled("Setup stopped. Already installed components were kept.")


def download_installer(url: str, path: Path, report, cancel: threading.Event):
    _cancelled(cancel)
    opener = build_opener(_HttpsRedirects())
    with opener.open(Request(url, headers={"User-Agent": "STALKER-GAMMA-Commander"}), timeout=30) as response, path.open("wb") as target:
        total = int(response.headers.get("Content-Length", 0))
        received = 0
        last_percent = -1
        while True:
            _cancelled(cancel)
            block = response.read(256 * 1024)
            if not block:
                break
            received += len(block)
            if received > 256 * 1024 * 1024:
                raise OSError("The runtime installer exceeds the expected download size.")
            target.write(block)
            percent = int(received * 100 / total) if total else int(received / 1024**2)
            if percent != last_percent:
                report(f"Downloading {path.name}: {percent}{'%' if total else ' MB'}")
                last_percent = percent
        if not received or (total and received != total):
            raise OSError("The runtime installer download was incomplete.")


def verify_microsoft_signature(path: Path):
    # Paths travel through the environment, never interpolated into shell code.
    script = """$ErrorActionPreference = 'Stop'
Import-Module "$PSHOME/Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1"
$signature = Get-AuthenticodeSignature -LiteralPath $env:COMMANDER_RUNTIME_INSTALLER
[pscustomobject]@{status=$signature.Status.ToString();publisher=$signature.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)} | ConvertTo-Json -Compress
"""
    command = [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
               "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()]
    env = external_environment(dict(os.environ, COMMANDER_RUNTIME_INSTALLER=str(path)))
    # A launcher started from PowerShell 7 can inherit its incompatible modules.
    # Resolve the built-in security module from Windows PowerShell itself.
    env["PSModulePath"] = str(Path(command[0]).parent / "Modules")
    with external_dll_directory():
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        signature = json.loads(result.stdout)
    except (ValueError, TypeError):
        signature = {}
    if result.returncode or signature.get("status") != "Valid" or signature.get("publisher") != "Microsoft Corporation":
        raise OSError(f"Microsoft signature verification failed for {path.name}. Nothing was installed from this file.")


def run_elevated(path: Path, arguments: list[str]) -> int:
    """Wait for the Microsoft installer; never terminate a running MSI operation."""
    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100  # keep process handle, do not return before launch completes
    info.lpVerb = "runas"
    info.lpFile = str(path)
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.lpDirectory = str(path.parent)
    info.nShow = 1  # user explicitly requested the interactive Microsoft setup
    with external_dll_directory():
        if not shell.ShellExecuteExW(ctypes.byref(info)):
            error = ctypes.get_last_error()
            if error == 1223:
                raise SetupCancelled("Windows elevation was cancelled. No further installers were started.")
            raise ctypes.WinError(error)
    try:
        if kernel.WaitForSingleObject(info.hProcess, 0xFFFFFFFF) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value
    finally:
        kernel.CloseHandle(info.hProcess)


def install_missing_runtimes(report, cancel: threading.Event) -> dict:
    """Called only after the user clicks Install missing in the setup dialog."""
    from .config import cli_binary_path

    checks = check_runtimes()
    needed = {check.key for check in checks if check.installed is not True}
    packages = [(f"Visual C++ ({arch})", VC_URL.format(arch=arch), f"vc_redist.{arch}.exe", False)
                for arch in ("x64", "x86") if "vc_" + arch in needed]
    if needed & {"dx_x64", "dx_x86"}:
        packages.append(("DirectX June 2010", DX_URL, "directx_Jun2010_redist.exe", True))
    reboot = False
    try:
        with tempfile.TemporaryDirectory(prefix="commander-runtimes-", ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            for name, url, filename, directx in packages:
                _cancelled(cancel)
                if game_running():
                    raise OSError("Close Mod Organizer and Anomaly before installing runtimes.")
                report(f"Downloading {name} from Microsoft...")
                installer = root / filename
                download_installer(url, installer, report, cancel)
                report(f"Verifying Microsoft's signature: {name}...")
                verify_microsoft_signature(installer)
                _cancelled(cancel)
                if directx:
                    report("Extracting DirectX setup...")
                    unpacked = root / "directx"
                    archiver = cli_binary_path().parent / "resources/7zz.exe"
                    with external_dll_directory():
                        result = subprocess.run([str(archiver), "x", str(installer), "-o" + str(unpacked), "-y"],
                                                capture_output=True, timeout=120, check=False,
                                                env=external_environment(dict(os.environ)),
                                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if result.returncode:
                        raise OSError("Could not extract the DirectX installer.")
                    installer = unpacked / "DXSETUP.exe"
                    verify_microsoft_signature(installer)
                _cancelled(cancel)
                if game_running():
                    raise OSError("Close Mod Organizer and Anomaly before installing runtimes.")
                report(f"Installing {name}. Accept the Windows permission prompt; finish the Microsoft setup window.")
                # DirectX's wizard handles consent and progress. VC's passive
                # mode suppresses automatic reboot but retains a progress window.
                code = run_elevated(installer, [] if directx else ["/install", "/passive", "/norestart"])
                if code in (1223, 1602):
                    raise SetupCancelled(f"{name} installation was cancelled.")
                if code not in (0, 3010, 1638):
                    raise OSError(f"{name} installer returned error {code}.")
                reboot |= code == 3010
                report(f"{name} setup finished" + ("; restart Windows when convenient." if code == 3010 else "."))
            _cancelled(cancel)
    except SetupCancelled as exc:
        return {"checks": check_runtimes(), "reboot": reboot, "cancelled": True, "message": str(exc)}
    return {"checks": check_runtimes(), "reboot": reboot, "cancelled": False, "message": ""}
