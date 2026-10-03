"""Run the stalker-gamma CLI as a subprocess.

Long-running commands (full-install, update apply, anomaly install/check) run in
a QThread so the UI stays responsive and output lines can be streamed into the
GUI. Quick commands use a simple synchronous helper.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .config import child_environment, cli_binary_path
from .launcher import _terminate_process_group as _terminate_group

if os.name == "nt":
    _CANCEL_SIGNAL = signal.CTRL_BREAK_EVENT
else:
    _CANCEL_SIGNAL = signal.SIGINT

#: Exit code reported when the CLI process could not be started or died
#: unexpectedly (mirrors the shell's "command not executable" convention).
SPAWN_FAILED_RC = 126
#: Exit code reported when a synchronous command exceeded its timeout.
TIMEOUT_RC = 124
_MAX_OUTPUT_LINES = 5000
_MAX_OUTPUT_CHARS = 1_000_000


def _bounded_output(text: str) -> str:
    """Keep command diagnostics bounded even when a subprocess is very noisy."""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return "[output truncated]\n" + text[-_MAX_OUTPUT_CHARS:]


#: The dynamic loader's complaint about an LD_PRELOAD entry it cannot load.
#: Started from Steam, COMMANDER inherits both the 32- and 64-bit
#: gameoverlayrenderer.so, so every child prints one of these for the
#: wrong-bitness copy - noise, but it lands in output callers parse.
_LD_PRELOAD_NOISE_RE = re.compile(
    r"ld\.so: object '[^']*' from LD_PRELOAD cannot be pre?loaded"
)


def _strip_loader_noise(text: str) -> str:
    """Drop the loader's LD_PRELOAD failure lines from captured output."""
    if "LD_PRELOAD" not in text:
        return text
    return "\n".join(
        line for line in text.split("\n") if not _LD_PRELOAD_NOISE_RE.search(line)
    )


def _query_environment() -> dict[str, str]:
    """Child environment for quick CLI commands, minus Steam's overlay.

    The overlay only matters to the game; for the CLI it just makes the
    loader print errors (see _LD_PRELOAD_NOISE_RE).
    """
    env = child_environment()
    preload = env.get("LD_PRELOAD")
    if preload:
        kept = [
            entry
            for entry in re.split(r"[:\s]+", preload)
            if entry and "gameoverlayrenderer" not in entry
        ]
        if kept:
            env["LD_PRELOAD"] = ":".join(kept)
        else:
            env.pop("LD_PRELOAD")
    return env


class CliWorker(QObject):
    """Runs one CLI invocation, streaming output lines.

    Usage: configure with ``setup()`` after moving to a QThread, then call the
    parameterless ``run()`` slot. ``cancel()`` signals the child process.
    """

    line_ready = Signal(str)
    finished = Signal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self._process: subprocess.Popen[str] | None = None
        self._command: list[str] = []
        self._cwd = ""
        self._env: dict[str, str] | None = None
        self._cancel_event = threading.Event()
        self._moddb_bridge = None
        self._pause_lock = threading.RLock()
        self._paused_tree = None

    def setup(
        self, command: list[str], cwd: str = "", env: dict[str, str] | None = None
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._env = env
        self._cancel_event.clear()

    @Slot()
    def run(self) -> None:
        try:
            self._run_command()
        finally:
            with self._pause_lock:
                if self._paused_tree is not None:
                    try:
                        self._paused_tree.resume()
                    except OSError as exc:
                        self.line_ready.emit(f"Could not release paused helpers: {exc}")
                    self._paused_tree = None
            if self._moddb_bridge is not None:
                self._moddb_bridge.close()
                self._moddb_bridge = None

    def _run_command(self) -> None:
        """Run the command, streaming stdout.

        Every failure path must still emit ``finished``: callers gate UI state
        (busy flags, disabled buttons) on that signal, so letting an exception
        escape this slot would leave the GUI permanently locked.
        """
        command = self._command
        cwd = self._cwd
        # Always an explicit environment: the CLI's helpers (umu-run,
        # protontricks, winetricks) are programs of their own and must not
        # inherit the AppImage's interpreter settings.
        env = child_environment()
        if self._env:
            env.update(self._env)
        # Optional protocol understood by our fork; older binaries ignore it.
        env["COMMANDER_PROGRESS_JSON"] = "1"
        collected: deque[str] = deque(maxlen=_MAX_OUTPUT_LINES)
        if not command:
            self.finished.emit(SPAWN_FAILED_RC, "No command specified")
            return
        try:
            if os.name == "nt":
                from .moddb_session import ACCESS_PREFIX, STATUS_PREFIX, ModDbBridge

                self._moddb_bridge = ModDbBridge(
                    lambda message: self.line_ready.emit(STATUS_PREFIX + message),
                    access_changed=lambda state: self.line_ready.emit(ACCESS_PREFIX + json.dumps(state)),
                )
                env.update(self._moddb_bridge.start())
            self._process = subprocess.Popen(
                command,
                cwd=cwd or None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
        except (OSError, ValueError) as exc:
            self._process = None
            name = command[0] if command else "<empty>"
            message = f"Failed to start {name!r}: {exc}"
            self.line_ready.emit(message)
            self.finished.emit(SPAWN_FAILED_RC, message)
            return
        if self._cancel_event.is_set():
            # Cancel arrived before the process spawned; apply it now.
            self.cancel()
        try:
            if self._process.stdout is None:
                raise RuntimeError(f"No stdout pipe for {command[0]!r}")
            for raw in self._process.stdout:
                line = raw.rstrip("\r\n")
                collected.append(line)
                self.line_ready.emit(line)
            self._process.wait()
            rc = self._process.returncode
        except Exception as exc:  # noqa: BLE001 - must not escape the slot
            message = f"Error while running {command[0]!r}: {exc}"
            collected.append(message)
            self.line_ready.emit(message)
            proc, self._process = self._process, None
            if proc is not None and proc.poll() is None:
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        _terminate_group(proc.pid, proc)
                except OSError:
                    try:
                        proc.kill()
                    except OSError:
                        pass
            self.finished.emit(SPAWN_FAILED_RC, _bounded_output("\n".join(collected)))
            return
        self._process = None
        self.finished.emit(rc, _bounded_output("\n".join(collected)))

    @Slot()
    def cancel(self) -> None:
        if self._moddb_bridge is not None:
            self._moddb_bridge.session.stop()
        proc = self._process
        if proc is None or proc.poll() is not None:
            # The process has not been spawned yet (or already exited); run()
            # checks _cancel_event right after Popen and cancels immediately.
            self._cancel_event.set()
            return
        self._cancel_event.set()
        pid = proc.pid
        try:
            if os.name == "nt":
                # A windowless GUI has no console to deliver CTRL_BREAK to.
                # Stop the owned tree, including download/extraction helpers.
                threading.Thread(target=_terminate_group, args=(pid, proc), daemon=True).start()
            else:
                os.killpg(pid, _CANCEL_SIGNAL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        threading.Thread(
            target=self._force_kill_after_cancel,
            args=(proc,),
            daemon=True,
        ).start()

    def _force_kill_after_cancel(self, proc: subprocess.Popen) -> None:
        time.sleep(3)
        # Identity check on the captured Popen object, not just its pid: if
        # this worker was reused for a new run() within the 3s window, a
        # pid-only match could hit a different, unrelated process if the OS
        # happened to recycle the same pid.
        if self._process is not proc or proc.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                _terminate_group(proc.pid, proc)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass

    def verify_moddb(self) -> bool:
        bridge = self._moddb_bridge
        return bridge.session.approve_verification() if bridge is not None else False

    def pause(self) -> bool:
        """Pause the owned CLI and helpers without discarding their state."""
        proc = self._process
        if proc is None or proc.poll() is not None or self._cancel_event.is_set():
            return False
        try:
            with self._pause_lock:
                if os.name == "nt":
                    from .windows import PausedProcessTree

                    if self._paused_tree is None:
                        self._paused_tree = PausedProcessTree()
                    return self._paused_tree.suspend(proc)
                os.killpg(proc.pid, signal.SIGSTOP)
                return True
        except OSError as exc:
            self.line_ready.emit(f"@commander-status Pause failed: {exc}")
            return False

    def resume(self) -> bool:
        """Resume the same processes, including any suspended extractors."""
        proc = self._process
        if proc is None or proc.poll() is not None:
            return False
        try:
            with self._pause_lock:
                if os.name == "nt":
                    return self._paused_tree.resume() if self._paused_tree is not None else True
                os.killpg(proc.pid, signal.SIGCONT)
                return True
        except OSError as exc:
            self.line_ready.emit(f"@commander-status Resume failed: {exc}")
            return False

    @Slot()
    def kill(self) -> None:
        if self._moddb_bridge is not None:
            self._moddb_bridge.session.stop()
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    _terminate_group(proc.pid, proc)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass


def _as_text(value: str | bytes | None) -> str:
    """Normalise captured output to text.

    ``TimeoutExpired`` carries the raw *bytes* read so far even when the
    process was started in text mode, so this cannot assume ``str``.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Stop a timed-out command and its descendants where the platform allows."""
    _terminate_group(proc.pid, proc)


def run_sync(
    args: list[str],
    cwd: str | Path | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """Run a quick CLI command synchronously, returning (exit code, combined output)."""
    cmd = [str(cli_binary_path()), *args]
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=_query_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            _terminate_process_group(proc)
            try:
                # The group should be dead after termination, but an
                # unkillable/signal-ignoring child must not hang the caller.
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = exc.stdout, exc.stderr
        else:
            stdout, stderr = exc.stdout, exc.stderr
        output = _bounded_output(
            _as_text(stdout) + _strip_loader_noise(_as_text(stderr))
        )
        return TIMEOUT_RC, f"{output}\n[timed out after {timeout}s]"
    except OSError as exc:
        return SPAWN_FAILED_RC, f"Failed to start {cmd[0]!r}: {exc}"
    return proc.returncode, _bounded_output(
        stdout + "\n" + _strip_loader_noise(stderr)
    )


def cli_command(
    args: list[str], *, progress_interval_ms: int | None = None
) -> list[str]:
    """Build the full command line for a CLI invocation."""
    cmd = [str(cli_binary_path()), *args]
    # The pinned upstream Windows 1.35.0 CLI emits progress without this
    # Commander-specific option; passing it causes command validation to fail.
    if progress_interval_ms is not None and os.name != "nt":
        cmd += ["--progress-update-interval-ms", str(progress_interval_ms)]
    return cmd
