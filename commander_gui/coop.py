"""Managed xrRazom profiles and reversible engine overlays.

All mutations are journalled before touching game files. Package payloads and
original files live outside GAMMA so a GAMMA reinstall cannot erase recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .atomic import write_bytes, write_text
from .config import settings_dir

PROFILE = "G.A.M.M.A. Co-op"
MOD = "xrRazom Co-op"
SLIM_PAGE = "https://www.moddb.com/mods/stalker-anomaly/addons/xrrazom-stalker-anomaly-co-op-slim"
SLIM_MD5 = "3e33cc23314123b8a8cbf79457fb2403"
SLIM_VERSION = "1.4"
SLIM_START = "https://www.moddb.com/addons/start/314090"


class CoopError(RuntimeError):
    pass


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(
        p in {".", ".."} or ":" in p for p in path.parts
    ):
        raise CoopError(f"Unsafe co-op path: {relative}")
    target = root / path
    # Reject directory junctions as well as symlinks, including ancestors.
    for part in (target, *target.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise CoopError(f"Linked paths are not supported for co-op: {part}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise CoopError(f"Path leaves co-op root: {relative}")
    return target


def ini_set(text: str, section: str, values: dict[str, str]) -> str:
    """Edit only named keys, preserving unrelated MO2/MCM configuration."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == f"[{section.lower()}]"), None)
    if start is None:
        lines.extend(["", f"[{section}]", *(f"{k}={v}" for k, v in values.items())])
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        pending = dict(values)
        for i in range(start + 1, end):
            key = lines[i].partition("=")[0].strip()
            if key in values:
                lines[i] = f"{key}={values[key]}"
                pending.pop(key, None)
        lines[end:end] = [f"{k}={v}" for k, v in pending.items()]
    return "\n".join(lines) + "\n"


def enable_mod(text: str) -> str:
    lines = [l for l in text.splitlines() if l.lstrip("+-").casefold() != MOD.casefold()]
    # MO2 writes highest priority first in modlist.txt.
    return "+" + MOD + "\n" + "\n".join(lines) + "\n"


class CoopManager:
    def __init__(self, profile, *, storage: Path | None = None, running=None):
        self.profile = profile
        self.gamma = Path(profile.gamma).absolute()
        self.anomaly = Path(profile.anomaly).absolute()
        key = hashlib.sha256(str(self.anomaly.resolve()).casefold().encode()).hexdigest()[:24]
        self.root = (storage or settings_dir() / "coop") / key
        self.state_path = self.root / "state.json"
        self.journal_path = self.root / "transaction.json"
        if running is None:
            from .windows import game_running
            running = game_running
        self.running = running

    def state(self) -> dict:
        if not self.state_path.exists():
            return {}
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("gamma") != str(self.gamma) or state.get("anomaly") != str(self.anomaly):
            raise CoopError("This Anomaly engine is already managed by another GAMMA installation.")
        return state

    def coop_profile_name(self, state=None) -> str:
        state = self.state() if state is None else state
        return state.get("coop_profile", PROFILE)

    @staticmethod
    def validate_profile_name(name: str) -> str:
        if (not isinstance(name, str) or not name or name != name.strip() or len(name) > 100
                or name.endswith(".") or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
                or name in {".", ".."} or re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name)):
            raise CoopError("Choose a valid MO2 profile name without path separators or Windows reserved characters.")
        return name

    def validate_coop_profile(self, state=None, *, require_mod=True) -> str:
        state = self.state() if state is None else state
        if not self.profile.mo2_coop_profile:
            raise CoopError("No Co-op MO2 profile configured. Select its profile in Profiles or install co-op first.")
        name = self.validate_profile_name(self.profile.mo2_coop_profile)
        if name.casefold() == self.profile.singleplayer_profile.casefold():
            raise CoopError("Single-player and Co-op must use different MO2 profiles.")
        if name.casefold() != self.coop_profile_name(state).casefold():
            raise CoopError("The configured Co-op profile does not match this managed installation.")
        path = safe_path(self.gamma, f"profiles/{name}/modlist.txt")
        if not path.is_file():
            raise CoopError("The Co-op MO2 profile is missing or invalid. Restore its modlist or select its valid profile in Profiles.")
        if require_mod:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            first = next((line for line in lines if line.startswith("+")), "")
            if first != "+" + MOD:
                raise CoopError("xrRazom must be enabled at highest priority. Activate Co-op again to repair its priority.")
            payload = safe_path(self.gamma, f"mods/{MOD}/gamedata")
            if not payload.is_dir():
                raise CoopError("The xrRazom mod is missing. Repair co-op before playing.")
        return name

    def _path(self, key: str) -> Path:
        kind, relative = key.split("/", 1)
        roots = {"game": self.anomaly, "gamma": self.gamma, "store": self.root}
        if kind not in roots:
            raise CoopError("Invalid recovery root")
        return safe_path(roots[kind], relative)

    def _blob(self, data: bytes) -> str:
        name = hashlib.sha256(data).hexdigest()
        path = safe_path(self.root, "objects/" + name)
        if not path.exists() or digest(path) != name:
            write_bytes(path, data)
        return name

    def _content(self, name: str | None) -> bytes | None:
        if name is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", name):
            raise CoopError("Invalid recovery object")
        data = safe_path(self.root, "objects/" + name).read_bytes()
        if hashlib.sha256(data).hexdigest() != name:
            raise CoopError("Co-op backup is damaged; files were not replaced.")
        return data

    @contextmanager
    def locked(self, *, recover=False):
        from PySide6.QtCore import QLockFile
        safe_path(self.root, "state.json")
        self.root.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.root / "operation.lock"))
        if not lock.tryLock(0):
            raise CoopError("Another co-op operation is running.")
        try:
            if self.running():
                raise CoopError("Close Anomaly and Mod Organizer 2 before changing co-op files.")
            self.state()  # Detect another install sharing the engine.
            if self.journal_path.exists() and not recover:
                raise CoopError("An interrupted co-op operation needs recovery. Use Recover on the Co-op page.")
            yield
        finally:
            lock.unlock()

    def _transaction(self, changes: dict[str, bytes | None], state: dict):
        transaction = uuid.uuid4().hex
        entries = []
        for key, data in changes.items():
            target = self._path(key)
            previous = target.read_bytes() if target.is_file() else None
            if target.exists() and not target.is_file():
                raise CoopError(f"Expected a file: {target}")
            entries.append({"path": key, "before": self._blob(previous) if previous is not None else None,
                            "after": self._blob(data) if data is not None else None})
        write_text(self.journal_path, json.dumps({"id": transaction, "entries": entries}))
        try:
            for entry in entries:
                self._put(entry["path"], entry["after"])
            state.update(gamma=str(self.gamma), anomaly=str(self.anomaly), transaction=transaction)
            write_text(self.state_path, json.dumps(state, indent=2))
        except Exception:
            self._recover()
            raise
        self.journal_path.unlink()

    def _put(self, key, content):
        target = self._path(key)
        data = self._content(content)
        if data is None:
            target.unlink(missing_ok=True)
        else:
            write_bytes(target, data)

    def _recover(self):
        journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if self.state().get("transaction") != journal["id"]:
            # Preflight every file before rolling anything back.
            for entry in journal["entries"]:
                path = self._path(entry["path"])
                current = digest(path) if path.is_file() else None
                if current not in (entry["before"], entry["after"]):
                    raise CoopError(f"Recovery needs attention: {path} changed outside Commander. Backups are in {self.root}")
                self._content(entry["before"])
            for entry in reversed(journal["entries"]):
                self._put(entry["path"], entry["before"])
        self.journal_path.unlink()

    def recover(self):
        with self.locked(recover=True):
            if self.journal_path.exists():
                self._recover()
        return "Recovery complete."

    def _validate_layout(self):
        ini = safe_path(self.gamma, "ModOrganizer.ini")
        text = ini.read_text(encoding="utf-8-sig")
        match = re.search(r"^gamePath=(.+)$", text, re.MULTILINE)
        if not match:
            raise CoopError("MO2 gamePath is missing from ModOrganizer.ini.")
        value = match[1].strip()
        if value.startswith("@ByteArray(") and value.endswith(")"):
            value = value[11:-1]
        value = value.strip('"').replace("\\\\", "\\")
        if Path(value).resolve() != self.anomaly.resolve():
            raise CoopError("Commander's Anomaly folder differs from MO2's gamePath. Correct it in Settings first.")

    def install_zip(self, archive: Path, *, coop_profile=None, adopt=False, report=lambda _: None, cancel=None):
        from .mod_install import extract_archive
        report("Checking xrRazom Slim archive…")
        md5 = hashlib.md5()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                if cancel is not None and cancel.is_set():
                    raise CoopError("Installation cancelled")
                md5.update(block)
        if md5.hexdigest() != SLIM_MD5:
            raise CoopError("Select the official xrRazom 1.4 Slim ZIP. Its publisher MD5 must match; Full Bundle and other releases are not supported by this integration yet.")
        self.root.mkdir(parents=True, exist_ok=True)
        cached = safe_path(self.root, f"downloads/xrRazom-{SLIM_VERSION}-Slim.zip")
        if archive.resolve() != cached.resolve():
            cached.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=cached.parent, suffix=".zip")
            os.close(fd)
            try:
                shutil.copyfile(archive, temporary)
                os.replace(temporary, cached)
            finally:
                Path(temporary).unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="staging-", dir=self.root) as tmp:
            staged = Path(tmp) / "payload"
            extract_archive(archive, staged, cancel_event=cancel,
                            progress=lambda percent, text: report(text))
            if cancel is not None and cancel.is_set():
                raise CoopError("Installation cancelled")
            result = self.install_payload(staged, adopt=adopt, report=report, coop_profile=coop_profile)
        return result

    def install_payload(self, package: Path, *, coop_profile=None, adopt=False, report=lambda _: None):
        """Install a previously authenticated archive (separate for offline tests)."""
        with self.locked():
            self._validate_layout()
            old = self.state()
            if old.get("maintenance"):
                raise CoopError("Finish or repair the interrupted GAMMA update before reinstalling co-op.")
            target_name = self.validate_profile_name(coop_profile or self.profile.mo2_coop_profile or self.coop_profile_name(old))
            if old.get("installed") and target_name.casefold() != self.coop_profile_name(old).casefold():
                raise CoopError("Remove the current co-op installation before creating a different Co-op profile.")
            source_name = self.profile.singleplayer_profile
            if source_name == self.profile.mo2_coop_profile:
                source_name = old.get("source_profile", source_name)
            self.validate_profile_name(source_name)
            if source_name.casefold() == target_name.casefold():
                raise CoopError("Select the single-player profile to clone first.")
            source = safe_path(self.gamma, f"profiles/{source_name}/modlist.txt").parent
            if not (source / "modlist.txt").is_file():
                raise CoopError("The selected GAMMA profile has no modlist.txt.")
            destination = safe_path(self.gamma, f"profiles/{target_name}/modlist.txt").parent
            managed = bool(old.get("source_profile")) and target_name.casefold() == self.coop_profile_name(old).casefold()
            existing = (destination.exists() and any(destination.iterdir())) or (self.gamma / "mods" / MOD).exists()
            if existing and not managed and not adopt:
                raise CoopError("A co-op profile or mod already exists. Choose an unused profile name; adopting existing setups is not currently available.")
            changes = {}
            if not (destination / "modlist.txt").exists():
                for file in source.iterdir():
                    if file.is_file() and not file.name.startswith("."):
                        safe_path(source, file.name)
                        changes[f"gamma/profiles/{target_name}/{file.name}"] = file.read_bytes()
            def profile_text(name):
                key = f"gamma/profiles/{target_name}/{name}"
                value = changes.get(key)
                path = self._path(key)
                return value.decode("utf-8-sig") if value is not None else (path.read_text(encoding="utf-8-sig") if path.exists() else "")
            changes[f"gamma/profiles/{target_name}/modlist.txt"] = enable_mod(profile_text("modlist.txt")).encode()
            changes[f"gamma/profiles/{target_name}/settings.ini"] = ini_set(profile_text("settings.ini"), "General", {"LocalSaves": "true", "LocalSettings": "true"}).encode()
            payload = {}
            for folder, prefix in (("Anomaly/bin", "game/bin"), ("Anomaly/gamedata", "game/gamedata"), ("gamedata", f"gamma/mods/{MOD}/gamedata")):
                directory = safe_path(package, folder)
                files = list(directory.rglob("*")) if directory.is_dir() else []
                count = 0
                for file in files:
                    safe_path(package, file.relative_to(package).as_posix())
                    if file.is_file():
                        key = prefix + "/" + file.relative_to(directory).as_posix()
                        payload[key] = self._blob(file.read_bytes())
                        count += 1
                if not count:
                    raise CoopError(f"Archive is missing {folder}.")
            original = dict(old.get("original", {}))
            for key, payload_hash in payload.items():
                path = self._path(key)
                if key not in original:
                    if key.startswith("game/bin/") and key.lower().endswith(".exe") and path.is_file() and digest(path) == payload_hash:
                        raise CoopError("xrRazom engines are already installed outside Commander. Restore the GAMMA engines first so Commander can back up a working single-player engine.")
                    original[key] = self._blob(path.read_bytes()) if path.is_file() else None
            # Restore files removed in a later package, and keep immutable originals.
            for key, old_hash in old.get("payload", {}).items():
                if key not in payload:
                    path = self._path(key)
                    if path.is_file() and digest(path) != old_hash:
                        raise CoopError(f"Cannot remove modified co-op file: {path}")
                    changes[key] = self._content(original.get(key))
            for key, value in payload.items():
                changes[key] = self._content(value)
            # Retire the two old co-op archives only inside the managed mod.
            for name in ("00_modded_exes_gamedata.db0", "12_xrmpe_anims_gamedata.db0"):
                key = f"gamma/mods/{MOD}/db/mods/{name}"
                path = self._path(key)
                if path.is_file():
                    original.setdefault(key, self._blob(path.read_bytes()))
                    changes[key] = None
            # An adopted profile is preserved; all overwritten mod files have backups.
            report("Installing xrRazom and its managed co-op profile…")
            state = dict(old, installed=True, active=True, source_profile=source_name, coop_profile=target_name,
                         version=SLIM_VERSION, publisher_md5=SLIM_MD5, payload=payload,
                         original=original, maintenance=False)
            self._transaction(changes, state)
            self.profile.mo2_singleplayer_profile = source_name
            self.profile.mo2_coop_profile = target_name
        return "xrRazom installed. Co-op profile and engine are ready."

    def switch(self, active: bool):
        with self.locked():
            self._validate_layout()
            state = self.state()
            if not state.get("installed"):
                raise CoopError("Install xrRazom Slim first.")
            if state.get("maintenance"):
                raise CoopError("A GAMMA update was interrupted. Finish the update before activating co-op.")
            if active:
                self.validate_coop_profile(state, require_mod=False)
            else:
                solo = self.validate_profile_name(self.profile.singleplayer_profile)
                if not safe_path(self.gamma, f"profiles/{solo}/modlist.txt").is_file():
                    raise CoopError("The single-player MO2 profile is missing. Select a valid profile in Profiles.")
            changes = self._engine_changes(state, active)
            if active:
                path = self._path(f"gamma/profiles/{self.coop_profile_name()}/modlist.txt")
                changes[f"gamma/profiles/{self.coop_profile_name()}/modlist.txt"] = enable_mod(path.read_text(encoding="utf-8-sig")).encode()
            self._transaction(changes, dict(state, active=active))
        return self.coop_profile_name(state) if active else self.profile.singleplayer_profile

    def _engine_changes(self, state, active, *, refreshed=False):
        changes = {}
        for key, value in state["payload"].items():
            if not key.startswith("game/"):
                continue
            path = self._path(key)
            current = digest(path) if path.is_file() else None
            before = value if state.get("active") else state["original"][key]
            wanted = value if active else state["original"][key]
            if current not in (before, wanted) and not refreshed:
                raise CoopError(f"Engine file changed outside Commander: {path}. Run a GAMMA repair before switching modes.")
            changes[key] = self._content(wanted)
        return changes

    def save_options(self, name: str, host: bool, steam: bool, port: int, players: int, address: str):
        if not name.strip() or len(name) > 48 or any(ord(c) < 32 or c in '";' for c in name):
            raise CoopError("Use a player name of 1–48 characters without quotes, semicolons or line breaks.")
        if not 1 <= port <= 65535 or not 2 <= players <= 4:
            raise CoopError("Invalid port or player limit.")
        if len(address) > 255 or any(c in address for c in '\r\n;[]='):
            raise CoopError("Invalid LAN address.")
        with self.locked():
            state = self.state()
            if not state.get("installed"):
                raise CoopError("Install xrRazom first.")
            key = f"gamma/profiles/{self.coop_profile_name()}/user.ltx"
            path = self._path(key)
            text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
            values = {"xrr_player_name": name.strip(), "xrr_host_session": "on" if host else "off",
                      "xrr_host_steam": "on" if steam else "off", "xrr_host_lan_port": str(port),
                      "xrr_max_players": str(players)}
            lines = [line for line in text.splitlines() if line.partition(" ")[0] not in values]
            lines.extend(f"{k} {v}" for k, v in values.items())
            # Give co-op its own highest-priority MCM settings; don't change shared
            # single-player MCM files. This also prevents the old migration overwriting
            # the chosen console settings on first launch.
            config_key = f"gamma/mods/{MOD}/gamedata/configs/axr_options.ltx"
            cfg = self._path(config_key)
            config = cfg.read_text(encoding="utf-8-sig") if cfg.exists() else self._effective_mcm(state)
            config = ini_set(config, "xrrazom", {"settings_migrated": "true", "last_ip": address})
            changes = {key: ("\n".join(lines) + "\n").encode(), config_key: config.encode()}
            state["options"] = {"name": name.strip(), "host": host, "steam": steam, "port": port, "players": players, "address": address}
            self._transaction(changes, state)
        return "Co-op settings saved."

    def options(self):
        """Prefer the game's latest persisted settings to launcher's old snapshot."""
        state = self.state()
        values = dict(state.get("options", {}))
        path = self._path(f"gamma/profiles/{self.coop_profile_name()}/user.ltx")
        if path.is_file():
            keys = {"xrr_player_name": "name", "xrr_host_session": "host", "xrr_host_steam": "steam",
                    "xrr_host_lan_port": "port", "xrr_max_players": "players"}
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                parts = line.split(maxsplit=1)
                if len(parts) != 2 or parts[0] not in keys:
                    continue
                key, value = keys[parts[0]], parts[1].strip()
                if key in {"host", "steam"}:
                    value = value.lower() in {"on", "true", "1"}
                elif key in {"port", "players"}:
                    try:
                        value = int(value)
                    except ValueError:
                        continue
                values[key] = value
        return values

    def _effective_mcm(self, state):
        modlist = self.gamma / "profiles" / state["source_profile"] / "modlist.txt"
        for line in modlist.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("+") and line[1:] != MOD:
                path = safe_path(self.gamma, f"mods/{line[1:]}/gamedata/configs/axr_options.ltx")
                if path.is_file():
                    return path.read_text(encoding="utf-8-sig")
        return ""

    def begin_maintenance(self):
        with self.locked():
            state = self.state()
            if not state.get("installed"):
                return False
            self._validate_layout()
            if state.get("maintenance"):
                return True  # Retry after an interrupted update, already on base engine.
            key = f"gamma/profiles/{self.coop_profile_name()}/modlist.txt"
            saved = self._path(key).read_bytes()
            state["resume_active"] = state.get("active", False)
            state["saved_modlist"] = self._blob(saved)
            changes = self._engine_changes(state, False, refreshed=True)
            self._transaction(changes, dict(state, active=False, maintenance=True))
            return True

    def finish_maintenance(self, success: bool):
        if not success:
            return  # Persistent marker blocks launching an incomplete update.
        with self.locked():
            state = self.state()
            if not state.get("maintenance"):
                return
            for key in state["payload"]:
                if key.startswith("game/"):
                    path = self._path(key)
                    state["original"][key] = self._blob(path.read_bytes()) if path.is_file() else None
            active = state.get("resume_active", False)
            changes = self._engine_changes(state, active, refreshed=True)
            # Restore managed payload too, in case GAMMA replaced or removed it.
            for key, value in state["payload"].items():
                if key.startswith("gamma/"):
                    changes[key] = self._content(value)
            key = f"gamma/profiles/{self.coop_profile_name()}/modlist.txt"
            old = self._content(state["saved_modlist"]).decode("utf-8-sig")
            names = {l.lstrip("+-").casefold() for l in old.splitlines()}
            path = self._path(key)
            after = path.read_text(encoding="utf-8-sig") if path.exists() else ""
            additions = [l for l in after.splitlines() if l.startswith(("+", "-")) and l[1:].casefold() not in names]
            changes[key] = enable_mod("\n".join(additions) + "\n" + old).encode()
            self._transaction(changes, dict(state, active=active, maintenance=False))

    def expected_files(self):
        state = self.state()
        if self.journal_path.exists() or state.get("maintenance"):
            raise CoopError("Co-op recovery or GAMMA maintenance must finish before integrity verification.")
        if not state.get("installed"):
            return {}, {}
        mods, engine = {}, {}
        for key, value in state["payload"].items():
            self._content(value)  # Validate reference store, never bless installed files.
            if key.startswith("gamma/"):
                mods[key[6:]] = ("sha256", value)
            elif state.get("active"):
                engine[key[5:]] = ("sha256", value)
        return mods, engine

    def uninstall(self):
        with self.locked():
            state = self.state()
            if not state.get("installed"):
                return self.profile.mo2_profile
            if state.get("maintenance"):
                raise CoopError("Finish GAMMA maintenance before removing co-op.")
            changes = self._engine_changes(state, False)
            for key, value in state["payload"].items():
                if key.startswith("gamma/"):
                    path = self._path(key)
                    if path.is_file() and digest(path) != value:
                        raise CoopError(f"Co-op file was modified: {path}. Save your changes before removal.")
                    changes[key] = self._content(state["original"].get(key))
            key = f"gamma/profiles/{self.coop_profile_name()}/modlist.txt"
            text = self._path(key).read_text(encoding="utf-8-sig")
            changes[key] = "\n".join("-" + MOD if l == "+" + MOD else l for l in text.splitlines()).encode() + b"\n"
            self._transaction(changes, dict(state, installed=False, active=False, payload={}, original={}))
        return self.profile.singleplayer_profile

    def repair(self):
        with self.locked():
            state = self.state()
            if not state.get("installed"):
                raise CoopError("Install xrRazom first.")
            if state.get("maintenance"):
                raise CoopError("Finish GAMMA maintenance first.")
            changes = {key: self._content(value) for key, value in state["payload"].items()
                       if key.startswith("gamma/") or state.get("active")}
            key = f"gamma/profiles/{self.coop_profile_name()}/modlist.txt"
            changes[key] = enable_mod(self._path(key).read_text(encoding="utf-8-sig")).encode()
            self._transaction(changes, state)
        return "Co-op files and load order repaired from the verified Slim package."

    def assert_launch(self, *, direct=False, binary=None, verify_files=True):
        state = self.state()
        if self.journal_path.exists() or state.get("maintenance"):
            raise CoopError("Complete co-op recovery / GAMMA repair before playing.")
        if not state.get("installed"):
            if self.profile.mo2_coop_profile and self.profile.mo2_profile == self.profile.mo2_coop_profile:
                raise CoopError("Install co-op before launching its MO2 profile.")
            return
        self._validate_layout()
        wants_coop = self.profile.mo2_profile.casefold() == self.coop_profile_name(state).casefold()
        if wants_coop != state.get("active", False):
            raise CoopError("Profile and engine differ. Select Co-op or Single-player on the Co-op page first.")
        if wants_coop:
            self.validate_coop_profile(state)
            if direct:
                raise CoopError("Co-op requires launching through Mod Organizer 2.")
            if binary and Path(binary).resolve() not in {self._path(k).resolve() for k in state["payload"] if k.startswith("game/bin/") and k.lower().endswith(".exe")}:
                raise CoopError("Choose an Anomaly engine included with xrRazom in Play's Target list.")
            if verify_files:
                self._engine_changes(state, True)  # Check hashes without writing.
            lines = (self.gamma / "profiles" / self.coop_profile_name() / "modlist.txt").read_text(encoding="utf-8-sig").splitlines()
            first = next((l for l in lines if l.startswith("+")), "")
            if first != "+" + MOD:
                raise CoopError("xrRazom must be enabled at highest priority. Activate Co-op again to repair its priority.")

    def manifest(self, report=lambda _: None):
        state = self.state()
        if not state.get("installed"):
            raise CoopError("Install co-op before exporting a session manifest.")
        lines = (self.gamma / "profiles" / self.coop_profile_name() / "modlist.txt").read_text(encoding="utf-8-sig").splitlines()
        mods = [line[1:] for line in lines if line.startswith("+") and not line.endswith("_separator")]
        effective = {}
        def add_configs(root):
            for file in (root / "gamedata" / "configs").rglob("*"):
                safe_path(root, file.relative_to(root).as_posix())
                if file.is_file() and file.name.casefold() != "axr_options.ltx":
                    effective[file.relative_to(root).as_posix().casefold()] = digest(file)
        add_configs(self.anomaly)
        for name in reversed(mods):
            report(f"Checking session config: {name}")
            root = safe_path(self.gamma, "mods/" + name)
            if not root.is_dir():
                raise CoopError(f"Enabled mod is missing: {name}")
            add_configs(root)
        add_configs(safe_path(self.gamma, "overwrite"))
        return {"format": "commander-xrrazom-1", "version": state["version"], "publisher_md5": state["publisher_md5"],
                "mods": mods, "configs": effective}


def compare_manifests(local: dict, friend: dict) -> list[str]:
    if friend.get("format") != "commander-xrrazom-1":
        raise CoopError("Not a Commander co-op manifest.")
    differences = []
    if local.get("publisher_md5") != friend.get("publisher_md5"):
        differences.append("Different xrRazom releases")
    if local.get("mods") != friend.get("mods"):
        differences.append("Enabled mods or load order differ")
    mine, theirs = local.get("configs", {}), friend.get("configs", {})
    if not isinstance(theirs, dict):
        raise CoopError("Invalid manifest config list")
    for name in sorted(mine.keys() | theirs.keys()):
        if mine.get(name) != theirs.get(name):
            differences.append(f"Config differs: {name}")
    return differences


def begin_cli_maintenance(command):
    """Protect shared engines around game-changing commands, including repairs."""
    args = command[1:]
    if not (args[:1] == ["full-install"] or args[:2] in (["update", "apply"], ["anomaly", "install"])):
        return None, None
    from .settings import load_settings
    profile = load_settings().active_profile
    if profile is None:
        return None, None
    manager = CoopManager(profile)
    if not manager.state_path.exists() and not manager.journal_path.exists():
        return None, None
    from PySide6.QtCore import QLockFile
    lease = QLockFile(str(manager.root / "maintenance.lock"))
    if not lease.tryLock(0):
        raise CoopError("Another GAMMA maintenance operation is running.")
    try:
        if manager.begin_maintenance():
            return manager, lease
    except Exception:
        lease.unlock()
        raise
    lease.unlock()
    return None, None
