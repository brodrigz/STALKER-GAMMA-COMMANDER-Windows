"""Read-only verification against publisher archives and GitHub object hashes.

Never derives expected hashes from the installed files. Missing references make
the result incomplete. Archives are unpacked only into disposable staging.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request

from .integrity import Md5ScanResult
from .mod_install import extract_archive
from .moddb_session import ModDbSessionError, validate_url
from .network import read_response_bytes, urlopen_with_retry
from .repair import find_record_for_folder


class VerificationCancelled(Exception):
    pass


class VerificationStop:
    """Cancel peer workers after a fatal error without cancelling the UI event."""

    def __init__(self, requested):
        self.requested = requested
        self.stopped = threading.Event()
        self.failure = None

    def is_set(self):
        return self.stopped.is_set() or self.requested.is_set()

    def wait(self, timeout):
        if self.is_set():
            return True
        # Used for short pacing/extractor waits; user cancellation stays bounded.
        return self.stopped.wait(timeout) or self.requested.is_set()


def bounded_results(items, work, workers, cancel):
    """Keep a small queue and consume completions, never wait on input order."""
    iterator = iter(items)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="verify")
    pending = set()
    try:
        def fill():
            while len(pending) < workers * 2:
                check_cancel(cancel)
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                pending.add(pool.submit(work, item))
        fill()
        while pending:
            check_cancel(cancel)
            done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                yield future.result()
            fill()
    except BaseException:
        cancel.stopped.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


class SharedReferences:
    """Compute each archive/tree once, including failures, across workers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.values = {}

    def get(self, key, create):
        with self.lock:
            future = self.values.get(key)
            owner = future is None
            if owner:
                future = self.values[key] = Future()
        if owner:
            try:
                future.set_result(create())
            except BaseException as exc:  # noqa: BLE001 - propagate to every waiter via the future
                future.set_exception(exc)
        return future.result(), owner


class VerificationReferences:
    """Publisher references retained only for one verify/repair operation."""

    def __init__(self):
        self.metadata = {}
        self.archives = {}
        self.files = {}


def file_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def merge_scan(target, part):
    target.archives_verified += part.archives_verified
    target.files_scanned += part.files_scanned
    for name in ("changed", "removed", "errors", "archive_bad_mods", "unavailable", "anomaly_changed"):
        getattr(target, name).extend(getattr(part, name))


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise VerificationCancelled()


def safe_relative(value):
    value = value.replace("\\", "/")
    if (not value or value.startswith("/") or ":" in value
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ValueError("Unsafe source path")
    return value


def digest_file(path, algorithm, cancel=None):
    check_cancel(cancel)
    before = path.stat()
    digest = hashlib.new("sha1" if algorithm == "git" else algorithm, usedforsecurity=False)
    if algorithm == "git":
        digest.update(f"blob {before.st_size}\0".encode())
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            check_cancel(cancel)
            digest.update(data)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("File changed while being verified")
    return digest.hexdigest()


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonical = ""
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "canonical" in attrs.get("rel", "").split():
            self.canonical = attrs.get("href", "")

    def handle_data(self, data):
        if data.strip():
            self.tokens.append(data.strip())

    def metadata(self):
        def value(label):
            index = self.tokens.index(label)
            return self.tokens[index + 1]
        name, md5 = value("Filename"), value("MD5 Hash").lower()
        if "/" in safe_relative(name) or not re.fullmatch(r"[a-f0-9]{32}", md5):
            raise ValueError("Invalid ModDB filename or checksum")
        return name, md5


def moddb_metadata(start_url, session):
    # Resolve the catalogue's exact file ID, not the newest file on an addon page.
    page = session.request(validate_url(start_url))
    if page.status != 200:
        raise ValueError(f"ModDB metadata returned HTTP {page.status}")
    parser = MetadataParser()
    parser.feed(page.body)
    try:
        return parser.metadata()
    except (ValueError, IndexError):
        canonical = validate_url(urljoin(start_url, parser.canonical))
        if canonical == start_url or not parser.canonical:
            raise ValueError("ModDB did not provide file metadata") from None
    page = session.request(canonical)
    if page.status != 200:
        raise ValueError(f"ModDB metadata returned HTTP {page.status}")
    parser = MetadataParser()
    parser.feed(page.body)
    return parser.metadata()


def github_tree(repo_url, ref):
    parsed = urlsplit(repo_url)
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or len(parts) != 2:
        raise ValueError("Source verification requires a GitHub repository URL")
    owner, repo = parts
    url = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/git/trees/{quote(ref, safe='')}?recursive=1"
    with urlopen_with_retry(Request(url, headers={"User-Agent": "GAMMA-Commander", "Accept": "application/vnd.github+json"}), timeout=30) as response:
        data = json.loads(read_response_bytes(response, 64 * 1024 * 1024))
    if data.get("truncated") or not isinstance(data.get("tree"), list):
        raise ValueError("GitHub returned an incomplete reference tree")
    result = {}
    for entry in data["tree"]:
        if entry["type"] == "tree":
            continue
        if entry["type"] != "blob" or entry.get("mode") not in ("100644", "100755"):
            raise ValueError("Source contains unsupported symlinks or submodules")
        sha = entry["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("Invalid GitHub object hash")
        result[safe_relative(entry["path"])] = ("git", sha)
    if not result:
        raise ValueError("Source tree is empty")
    return result


def installed_layout(files, instructions):
    """Mirror CLI ProcessInstructions/CleanExtractPath using a virtual file map."""
    files = dict(files)
    for instruction in instructions:
        prefix = safe_relative(instruction).rstrip("/") + "/"
        selected = {p: v for p, v in files.items() if p.startswith(prefix)}
        has_gamedata = any(p.startswith(prefix + "gamedata/") for p in selected)
        for path, value in selected.items():
            destination = ("" if has_gamedata else "gamedata/") + path[len(prefix):]
            files.pop(path)
            files[destination] = value
    return {p: v for p, v in files.items()
            if ("/" not in p and not p.startswith("."))
            or p.split("/")[0].lower() in {"gamedata", "appdata", "db", "fomod"}}


@dataclass
class SourceScanResult(Md5ScanResult):
    archives_verified: int = 0
    archive_bad_mods: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    anomaly_changed: list[str] = field(default_factory=list)

    @property
    def complete(self):
        return not self.cancelled and not self.unavailable

    @property
    def problems(self):
        return super().problems + len(self.archive_bad_mods) + len(self.anomaly_changed)

    @property
    def summary(self):
        return (f"Source verification: {self.archives_verified} archives verified, "
                f"{self.files_scanned} installed files checked, {self.problems} differences, "
                f"{len(self.unavailable)} checks unavailable")

    def lines(self):
        return ([self.summary]
                + [f"CHANGED  {p}" for p in self.changed + self.anomaly_changed]
                + [f"MISSING  {p}" for p in self.removed]
                + [f"ARCHIVE MISMATCH  {p}" for p in self.archive_bad_mods]
                + [f"ERROR  {p}" for p in self.errors]
                + [f"NOT VERIFIED  {p}" for p in self.unavailable] + self.notes)


def compare_files(root, expected, result, cancel, report, *, anomaly=False,
                  references=None, force_check=()):
    for index, (relative, (algorithm, wanted)) in enumerate(expected.items(), 1):
        check_cancel(cancel)
        path = root / safe_relative(relative)
        if not path.resolve().is_relative_to(root.resolve()):
            result.errors.append(f"Unsafe installed path: {relative}")
            continue
        display = ("Anomaly/" if anomaly else "") + relative
        try:
            if not path.is_file():
                (result.anomaly_changed if anomaly else result.removed).append(display)
            else:
                identity = file_identity(path)
                key = (str(path), algorithm, wanted)
                cached = references.files.get(key) if references is not None else None
                if relative.casefold() in force_check or cached != identity:
                    if digest_file(path, algorithm, cancel) != wanted:
                        (result.anomaly_changed if anomaly else result.changed).append(display)
                    elif references is not None and file_identity(path) == identity:
                        references.files[key] = identity
            result.files_scanned += 1
        except OSError as exc:
            result.errors.append(f"{display}: {exc}")
        if index % 50 == 0 or index == len(expected):
            report(f"Source hashing {index}/{len(expected)} files")


def verify_sources(profile, records, session, cancel, report,
                   *, tree_fetch=github_tree, metadata_fetch=moddb_metadata,
                   extract=extract_archive, references=None, repair_folders=()):
    references = references if references is not None else VerificationReferences()
    result = SourceScanResult()
    requested_cancel = cancel
    cancel = VerificationStop(cancel)
    workers = max(1, min(3, int(profile.download_threads)))
    gamma = Path(profile.gamma)
    expected, anomaly_expected = {}, {}
    overlays = {}
    trees = SharedReferences()
    def tree(url, ref):
        check_cancel(cancel)
        def fetch():
            report(f"Reading source hashes: {url} ({ref})")
            return tree_fetch(url, ref)
        return trees.get((url, ref), fetch)[0]
    try:
        # Overlay order must match GammaInstaller.InstallAsync. Fetch all before
        # comparing archives, otherwise legitimate patches look like corruption.
        specs = [
            (profile.gamma_setup_repo_url, profile.gamma_setup_repo_branch, "modpack_addons/"),
            (profile.stalker_gamma_repo_url, profile.stalker_gamma_repo_branch, "G.A.M.M.A/modpack_addons/"),
            (profile.gamma_large_files_repo_url, profile.gamma_large_files_repo_branch, ""),
        ]
        for url, ref, prefix in specs:
            try:
                files = tree(url, ref)
                for path, value in files.items():
                    if path.startswith(prefix):
                        overlays["mods/" + path[len(prefix):]] = value
                    if url == profile.stalker_gamma_repo_url and path.startswith("G.A.M.M.A/modpack_patches/"):
                        rel = path.removeprefix("G.A.M.M.A/modpack_patches/")
                        # Settings are deliberately preserved/edited by install and play.
                        if (not rel.startswith(("appdata/", "profiles/"))
                                and rel.lower() not in {"bin/dxgi.dll", "bin/d3d9.dll"}):
                            anomaly_expected[rel] = value
            except (OSError, ValueError, KeyError) as exc:
                result.unavailable.append(f"GAMMA patches: {url}: {exc}")
        try:
            files = tree(profile.teivaz_anomaly_gunslinger_repo_url, profile.teivaz_anomaly_gunslinger_repo_branch)
            folders = sorted({p[:p.index("gamedata/")] for p in files if "gamedata/" in p})
            for folder in folders:
                prefix = folder + "gamedata/"
                for path, value in files.items():
                    if path.startswith(prefix):
                        overlays["mods/312- Gunslinger Guns for Anomaly - Teivazcz & Gunslinger Team/gamedata/" + path[len(prefix):]] = value
        except (OSError, ValueError, KeyError) as exc:
            result.unavailable.append(f"Gunslinger patches: {exc}")
        overlays_known = not result.unavailable
        # Only check mods actually installed; the presence pass handles enabled
        # missing mods. Disabled, intentionally uninstalled mods aren't corrupt.
        folders = [p.name for p in (gamma / "mods").iterdir() if p.is_dir() and not p.is_symlink()]
        matched = [(folder, find_record_for_folder(folder, records)) for folder in folders]
        metadata = SharedReferences()
        metadata_lock = threading.Lock()
        archive_payloads = SharedReferences()

        def fetch_metadata(url):
            # Only the network lane is serialized. Local verification continues
            # while another worker resolves its reference or waits for Cloudflare.
            with metadata_lock:
                if url in references.metadata:
                    return references.metadata[url]
                if cancel.wait(0.35):
                    raise VerificationCancelled()
                try:
                    value = metadata_fetch(url, session)
                    references.metadata[url] = value
                    return value
                except ModDbSessionError as exc:
                    # Stop queued requests immediately, before releasing the lock.
                    cancel.failure = exc
                    cancel.stopped.set()
                    raise

        def archive_payload(archive, md5, folder):
            identity = file_identity(archive)
            key = (str(archive), md5, identity)
            if key in references.archives:
                report(f"Reusing verified source hashes: {folder}")
                return references.archives[key]
            report(f"Checking archive: {folder}")
            if digest_file(archive, "md5", cancel) != md5:
                return None
            with tempfile.TemporaryDirectory(prefix="commander-verify-") as work:
                stage = Path(work) / "archive"
                report(f"Extracting reference: {folder}")
                extract(archive, stage, cancel_event=cancel)
                files = {}
                for path in stage.rglob("*"):
                    check_cancel(cancel)
                    if path.is_file():
                        files[path.relative_to(stage).as_posix()] = ("sha256", digest_file(path, "sha256", cancel))
                if file_identity(archive) != identity:
                    raise OSError("Archive changed while its reference was being prepared")
                references.archives[key] = files
                return files

        def verify_mod(item):
            folder, record = item
            check_cancel(cancel)
            part = SourceScanResult()
            payload_expected = {}
            report(f"Verifying source: {folder}")
            try:
                parsed = urlsplit(record.dl_link)
                if parsed.hostname in {"www.moddb.com", "moddb.com"}:
                    (name, md5), _ = metadata.get(record.dl_link, lambda: fetch_metadata(record.dl_link))
                    if "/" in safe_relative(name) or not re.fullmatch(r"[a-f0-9]{32}", md5):
                        raise ValueError("Invalid archive metadata")
                    record.zip_name, record.md5_mod_db = name, md5
                    record.checksum_known = True
                    archive = next((p for p in (gamma / "downloads" / name, Path(profile.cache) / name) if p.is_file()), None)
                    if archive is None:
                        part.unavailable.append(f"{folder}: archive missing ({name}); reinstall to restore it")
                        return part, payload_expected
                    files, owner = archive_payloads.get(
                        (str(archive), md5), lambda: archive_payload(archive, md5, folder))
                    if files is None:
                        part.archive_bad_mods.append(folder)
                        return part, payload_expected
                    part.archives_verified = int(owner)
                elif parsed.hostname == "github.com" and "/archive/" in parsed.path:
                    repo_path, ref = parsed.path.split("/archive/", 1)
                    ref = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
                    ref = ref.removesuffix(".zip").removesuffix(".tar.gz")
                    files = tree("https://github.com" + repo_path, ref)
                    archive_root = repo_path.rsplit("/", 1)[-1] + "-" + ref.replace("/", "-")
                    files = {archive_root + "/" + p: v for p, v in files.items()}
                else:
                    part.unavailable.append(f"{folder}: source has no supported publisher reference")
                    return part, payload_expected
                instructions = [s.strip() for s in record.instructions.split(":") if s.strip() and s.strip() != "0"]
                payload = installed_layout(files, instructions)
                if folder.casefold() != record.folder_name.casefold():
                    # Core patches target exact folder names. Do not compare a
                    # renamed mod against the unpatched archive or guess where
                    # its patches belong.
                    part.unavailable.append(f"{folder}: folder differs from catalogue ({record.folder_name}); installed-file comparison skipped")
                    return part, payload_expected
                if not payload:
                    part.unavailable.append(f"{folder}: no comparable installed payload")
                for path, value in payload.items():
                    if path.lower() != "meta.ini":  # generated by MO2/CLI
                        payload_expected[f"mods/{folder}/{path}"] = value
            except (OSError, ValueError, RuntimeError, IndexError) as exc:
                part.unavailable.append(f"{folder}: {exc}")
            return part, payload_expected

        jobs = [(folder, record) for folder, record in matched if record is not None]
        report(f"Verifying with {workers} local workers; ModDB requests are paced.")
        for completed, (part, payload_expected) in enumerate(bounded_results(jobs, verify_mod, workers, cancel), 1):
            merge_scan(result, part)
            expected.update(payload_expected)
            report(f"Verified sources {completed}/{len(jobs)}")
        if not records:
            result.unavailable.append("Mod catalogue unavailable or empty")
        check_cancel(cancel)
        if overlays_known:
            from .coop import CoopError, CoopManager
            try:
                coop_mods, coop_engine = CoopManager(profile).expected_files()
                expected.update(coop_mods)
                combined = {p.casefold(): (p, v) for p, v in anomaly_expected.items()}
                combined.update({p.casefold(): (p, v) for p, v in coop_engine.items()})
                anomaly_expected = dict(combined.values())
                if coop_mods:
                    result.notes.append("xrRazom files are checked against the authenticated Slim package retained by Commander.")
            except (OSError, ValueError, CoopError) as exc:
                result.unavailable.append(f"Co-op reference unavailable: {exc}")
            # Optional mods removed from disk must not be resurrected by checks.
            installed_folders = {folder.casefold() for folder in folders}
            overlays = {p: v for p, v in overlays.items() if p.split("/")[1].casefold() in installed_folders}
            # Windows paths are case insensitive; an overlay may change casing.
            merged = {p.casefold(): (p, v) for p, v in expected.items()}
            merged.update({p.casefold(): (p, v) for p, v in overlays.items()})
            expected = dict(merged.values())
            # Mutable settings and generated metadata aren't publisher content.
            expected = {p: v for p, v in expected.items()
                        if not p.lower().endswith(("/meta.ini", "/gamedata/configs/axr_options.ltx"))}
            repaired = {folder.casefold() for folder in repair_folders}
            force_check = {p.casefold() for p in overlays}
            force_check.update(p.casefold() for p in expected if p.split("/")[1].casefold() in repaired)
            def comparison_jobs():
                for root, entries, anomaly in ((gamma, expected, False), (Path(profile.anomaly), anomaly_expected, True)):
                    batch = {}
                    for path, reference in entries.items():
                        batch[path] = reference
                        if len(batch) == 64:
                            yield root, batch, anomaly
                            batch = {}
                    if batch:
                        yield root, batch, anomaly

            def compare_batch(job):
                root, entries, anomaly = job
                part = SourceScanResult()
                compare_files(root, entries, part, cancel, lambda _: None, anomaly=anomaly,
                              references=references,
                              force_check={p.casefold() for p in entries} if anomaly else force_check)
                return part

            total = len(expected) + len(anomaly_expected)
            for part in bounded_results(comparison_jobs(), compare_batch, workers, cancel):
                merge_scan(result, part)
                report(f"Source hashing {result.files_scanned}/{total} files")
        else:
            result.notes.append("Installed-file comparison skipped: patch references unavailable.")
        result.notes.append("Extra user files are left untouched; mutable appdata settings are excluded. References use the configured source versions.")
        result.notes.append("Unchanged files already checked against source in this operation reuse that result; repaired files and shared patches are hashed again.")
    except VerificationCancelled:
        if cancel.failure is not None and not requested_cancel.is_set():
            result.unavailable.append(str(cancel.failure))
        else:
            result.cancelled = True
    except ModDbSessionError as exc:
        if requested_cancel.is_set():
            result.cancelled = True
        else:
            result.unavailable.append(str(exc))
    return result
