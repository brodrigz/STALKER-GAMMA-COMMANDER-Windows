import hashlib
import threading
from pathlib import Path

import pytest

from commander_gui.moddb_session import ModDbRateLimitError, Page
from commander_gui.repair import ModPackRecord, classify_problems
from commander_gui.settings import CliProfile
from commander_gui.source_integrity import (
    SourceScanResult,
    installed_layout,
    moddb_metadata,
    safe_relative,
    verify_sources,
)


def sha(data):
    return "sha256", hashlib.sha256(data).hexdigest()


@pytest.fixture
def install(tmp_path):
    profile = CliProfile(gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"), cache=str(tmp_path / "cache"))
    record = ModPackRecord(1, "Example", "- Author", "https://www.moddb.com/addons/start/123", "", "", "", "Option")
    mod = Path(profile.gamma) / "mods" / record.folder_name
    (mod / "gamedata").mkdir(parents=True)
    Path(profile.cache).mkdir()
    Path(profile.anomaly).mkdir()
    archive = Path(profile.cache) / "mod.zip"
    archive.write_bytes(b"verified archive")
    (mod / "gamedata/a.script").write_bytes(b"original")
    md5 = hashlib.md5(archive.read_bytes()).hexdigest()
    def extract(archive, stage, **kwargs):
        (stage / "Option/gamedata").mkdir(parents=True)
        (stage / "Option/gamedata/a.script").write_bytes(b"original")
    def run(**kwargs):
        options = {"tree_fetch": lambda *_: {}, "metadata_fetch": lambda *_: ("mod.zip", md5), "extract": extract}
        options.update(kwargs)
        return verify_sources(profile, {record.folder_name: record}, None, threading.Event(), lambda _: None, **options)
    return profile, record, mod, archive, run


def test_checks_publisher_content_without_creating_local_baseline(install):
    profile, record, mod, _archive, run = install
    result = run()
    assert result.complete and result.problems == 0
    assert result.archives_verified == 1 and result.files_scanned == 1
    assert not (Path(profile.gamma) / "gamma-md5.txt").exists()
    (mod / "gamedata/a.script").write_bytes(b"corrupted before first scan")
    result = run()
    assert result.changed == [f"mods/{record.folder_name}/gamedata/a.script"]
    assert classify_problems(result, {record.folder_name: record}).has_repairable


def test_bad_archive_never_extracted_and_is_repairable(install):
    _profile, record, _mod, archive, run = install
    archive.write_bytes(b"bad")
    result = run(extract=lambda *a, **k: pytest.fail("untrusted archive extracted"))
    assert result.archive_bad_mods == [record.folder_name]
    assert result.problems == 1


def test_missing_archive_incomplete_not_success(install):
    _profile, _record, _mod, archive, run = install
    archive.unlink()
    result = run()
    assert not result.complete and result.unavailable
    assert result.archives_verified == 0


def test_patch_precedence_and_optional_mods(install):
    profile, record, mod, _archive, run = install
    (mod / "gamedata/a.script").write_bytes(b"patched")
    def tree(url, ref):
        if url == profile.stalker_gamma_repo_url:
            return {
                f"G.A.M.M.A/modpack_addons/{record.folder_name}/gamedata/a.script": sha(b"patched"),
                "G.A.M.M.A/modpack_addons/Optional absent/gamedata/x.script": sha(b"optional"),
                "G.A.M.M.A/modpack_patches/appdata/user.ltx": sha(b"settings"),
            }
        return {}
    result = run(tree_fetch=tree)
    assert result.complete and result.problems == 0


def test_unknown_patches_cannot_label_unpatched_reference_as_corruption(install):
    _profile, _record, mod, _archive, run = install
    (mod / "gamedata/a.script").write_bytes(b"patched")
    def unavailable(*_):
        raise OSError("offline")
    result = run(tree_fetch=unavailable)
    assert not result.complete and not result.changed
    assert result.archives_verified == 1


def test_source_engine_hash_is_checked(install):
    profile, _record, _mod, _archive, run = install
    binary = Path(profile.anomaly) / "bin/AnomalyDX11AVX.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"broken engine")
    def tree(url, ref):
        return {"G.A.M.M.A/modpack_patches/bin/AnomalyDX11AVX.exe": sha(b"engine")} if url == profile.stalker_gamma_repo_url else {}
    result = run(tree_fetch=tree)
    assert result.anomaly_changed == ["Anomaly/bin/AnomalyDX11AVX.exe"]
    assert result.problems == 1


def test_rate_limit_stops_metadata_requests(install):
    *_, run = install
    calls = []
    def metadata(*_):
        calls.append(True)
        raise ModDbRateLimitError("wait 33 minutes")
    result = run(metadata_fetch=metadata)
    assert calls == [True]
    assert not result.complete and "33 minutes" in result.unavailable[0]


def test_cancelled_hash_never_completes(install):
    profile, record, *_ = install
    cancel = threading.Event()
    cancel.set()
    result = verify_sources(profile, {record.folder_name: record}, None, cancel, lambda _: None)
    assert result.cancelled and not result.complete


def test_install_instructions_move_and_overlay_in_order():
    files = {"base/gamedata/a": sha(b"a"), "patch/a": sha(b"p"), "readme.txt": sha(b"r"), "other/b": sha(b"b")}
    result = installed_layout(files, ["base", "patch"])
    assert result == {"gamedata/a": sha(b"p"), "readme.txt": sha(b"r")}


@pytest.mark.parametrize("path", ["../x", "C:/x", "/x", "a/../x", "a\\..\\x"])
def test_unsafe_source_paths_rejected(path):
    with pytest.raises(ValueError):
        safe_relative(path)


def test_exact_moddb_start_id_metadata():
    class Session:
        def __init__(self):
            self.urls = []
        def request(self, url):
            self.urls.append(url)
            return Page(200, {}, '<link rel="canonical" href="/addons/exact-file">' if len(self.urls) == 1 else '<h5>Filename</h5><span>mod.zip</span><h5>MD5 Hash</h5><span>' + 'a' * 32 + '</span>')
    session = Session()
    assert moddb_metadata("https://www.moddb.com/addons/start/123", session) == ("mod.zip", "a" * 32)
    assert session.urls == ["https://www.moddb.com/addons/start/123", "https://www.moddb.com/addons/exact-file"]


def test_missing_references_are_not_clean():
    assert not SourceScanResult(unavailable=["no archive"]).complete


def test_counter_shift_does_not_compare_unpatched_archive(install):
    _profile, record, mod, _archive, run = install
    renamed = mod.with_name("999-" + record.folder_name.split("-", 1)[1])
    mod.rename(renamed)
    (renamed / "gamedata/a.script").write_bytes(b"legitimate patch")
    result = run()
    assert result.archives_verified == 1
    assert not result.complete and not result.changed
    assert any("folder differs from catalogue" in message for message in result.unavailable)


def test_github_archive_root_and_instructions(install):
    _profile, record, _mod, _archive, run = install
    record.dl_link = "https://github.com/example/mod/archive/refs/tags/latest.zip"
    record.instructions = "mod-latest/Option"
    result = run(tree_fetch=lambda url, _: {"Option/gamedata/a.script": sha(b"original")} if url == "https://github.com/example/mod" else {})
    assert result.complete and result.problems == 0 and result.files_scanned == 1


def test_verify_ui_does_not_pass_incomplete_scan(tmp_path):
    from unittest.mock import Mock, patch

    from PySide6.QtWidgets import QApplication

    from commander_gui.integrity import GammaVerifyResult
    from commander_gui.settings import CliSettings
    from commander_gui.ui.install_page import InstallPage

    app = QApplication.instance() or QApplication([])
    window = Mock()
    window.install_busy = False
    window.settings = CliSettings(profiles=[CliProfile(active=True, gamma=str(tmp_path))])
    page = InstallPage(window)
    page._verify_anomaly_ok = True
    scan = SourceScanResult(unavailable=["ModDB offline"])
    with patch.object(page, "_finish_verify") as finish:
        page._on_gamma_verify_done((GammaVerifyResult(), scan, None, {}, False, None))
        assert finish.call_args.kwargs["ok"] is False
    assert page.local_md5.button.text() == "Create / Check Local MD5"
    page.deleteLater()
    app.processEvents()


def test_post_repair_uses_source_verification_not_local_snapshot(tmp_path):
    from unittest.mock import Mock, patch

    from PySide6.QtWidgets import QApplication

    from commander_gui.integrity import GammaVerifyResult
    from commander_gui.settings import CliSettings
    from commander_gui.ui.install_page import InstallPage

    app = QApplication.instance() or QApplication([])
    window = Mock()
    window.install_busy = False
    window.settings = CliSettings(profiles=[CliProfile(active=True, gamma=str(tmp_path))])
    page = InstallPage(window)
    scan = SourceScanResult(changed=["mods/Test/a.script"])
    with (patch.object(page, "_scan_sources", return_value=scan) as verify,
          patch("commander_gui.ui.install_page.fetch_modpack_records", return_value={}),
          patch("commander_gui.ui.install_page.verify_gamma", return_value=GammaVerifyResult())):
        post, _ = page._run_post_scan(lambda _: None)
        assert post is scan
        verify.assert_called_once()
    assert not (tmp_path / "gamma-md5.txt").exists()
    page.deleteLater()
    app.processEvents()


def parallel_install(install, count=6, shared=False):
    from dataclasses import replace

    profile, original, mod, archive, _run = install
    records = {}
    for index in range(count):
        record = replace(original, counter=index + 1, addon_name=f"Mod {index}",
                         dl_link=f"https://www.moddb.com/addons/start/{0 if shared else index}")
        folder = mod.parent / record.folder_name / "gamedata"
        folder.mkdir(parents=True)
        (folder / "a.script").write_bytes(b"original")
        (archive.parent / f"{index}.zip").write_bytes(b"verified archive")
        records[record.folder_name] = record
    md5 = hashlib.md5(archive.read_bytes()).hexdigest()
    return profile, records, lambda url, _: (url.rsplit("/", 1)[1] + ".zip", md5)


def test_archive_work_overlaps_with_bounded_concurrency(install):
    profile, records, metadata = parallel_install(install)
    profile.download_threads = 20  # Local I/O still caps at three workers.
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    active = peak = calls = 0

    def extract(archive, stage, **kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            peak = max(peak, active)
            calls += 1
            first_wave = calls <= 3
        try:
            if first_wave:
                barrier.wait(timeout=8)
            (stage / "Option/gamedata").mkdir(parents=True)
            (stage / "Option/gamedata/a.script").write_bytes(b"original")
        finally:
            with lock:
                active -= 1

    result = verify_sources(profile, records, None, threading.Event(), lambda _: None,
                            tree_fetch=lambda *_: {}, metadata_fetch=metadata, extract=extract)
    assert result.complete and result.problems == 0
    assert peak == 3 and active == 0 and calls == 6
    assert result.archives_verified == 6 and result.files_scanned == 6


def test_shared_archive_and_metadata_are_processed_once(install):
    profile, records, metadata = parallel_install(install, shared=True)
    fetched, extracted = [], []

    def fetch(url, session):
        fetched.append(url)
        return metadata(url, session)

    def extract(archive, stage, **kwargs):
        extracted.append(archive)
        (stage / "Option/gamedata").mkdir(parents=True)
        (stage / "Option/gamedata/a.script").write_bytes(b"original")

    result = verify_sources(profile, records, None, threading.Event(), lambda _: None,
                            tree_fetch=lambda *_: {}, metadata_fetch=fetch, extract=extract)
    assert result.complete and result.problems == 0
    assert result.files_scanned == 6 and result.archives_verified == 1
    assert len(fetched) == len(extracted) == 1


def test_parallel_rate_limit_stops_queued_requests_and_preserves_reason(install):
    profile, records, _ = parallel_install(install, count=12)
    calls = []
    def limited(*args):
        calls.append(True)
        raise ModDbRateLimitError("wait 33 minutes")
    result = verify_sources(profile, records, None, threading.Event(), lambda _: None,
                            tree_fetch=lambda *_: {}, metadata_fetch=limited)
    assert calls == [True]
    assert not result.complete and not result.cancelled
    assert any("33 minutes" in reason for reason in result.unavailable)


def test_parallel_cancellation_joins_extractors_and_cleans_staging(install):
    profile, records, metadata = parallel_install(install)
    cancel = threading.Event()
    stages = []
    def extract(archive, stage, **kwargs):
        stage.mkdir()
        stages.append(stage)
        cancel.set()
    result = verify_sources(profile, records, None, cancel, lambda _: None,
                            tree_fetch=lambda *_: {}, metadata_fetch=metadata, extract=extract)
    assert result.cancelled and not result.complete
    assert stages and all(not stage.parent.exists() for stage in stages)


def test_slow_first_job_does_not_block_completions_or_expand_queue():
    from commander_gui.source_integrity import VerificationStop, bounded_results

    release = threading.Event()
    started = threading.Event()
    consumed = []
    def items():
        for index in range(100):
            consumed.append(index)
            yield index
    def work(index):
        if index == 0:
            started.set()
            assert release.wait(8)
        else:
            assert started.wait(8)
        return index
    results = bounded_results(items(), work, 2, VerificationStop(threading.Event()))
    try:
        assert next(results) != 0
        assert len(consumed) <= 4
    finally:
        release.set()
        results.close()


def test_installed_file_hashing_runs_in_parallel(install, monkeypatch):
    from commander_gui import source_integrity

    profile, record, mod, _archive, run = install
    overlay = {}
    for index in range(192):
        relative = f"gamedata/file{index}.script"
        (mod / relative).write_bytes(b"patched")
        overlay[f"G.A.M.M.A/modpack_addons/{record.folder_name}/{relative}"] = sha(b"patched")
    barrier = threading.Barrier(3)
    threads = set()
    lock = threading.Lock()
    original_digest = source_integrity.digest_file
    def digest(path, algorithm, cancel=None):
        if path.is_relative_to(mod):
            identity = threading.get_ident()
            with lock:
                first = identity not in threads
                threads.add(identity)
            if first:
                barrier.wait(timeout=8)
        return original_digest(path, algorithm, cancel)
    monkeypatch.setattr(source_integrity, "digest_file", digest)
    result = run(tree_fetch=lambda url, _: overlay if url == profile.stalker_gamma_repo_url else {})
    assert result.complete and result.problems == 0
    assert result.files_scanned == 193 and len(threads) == 3


def test_repair_reuses_publisher_references_without_extracting_healthy_archives(install):
    from commander_gui.source_integrity import VerificationReferences
    profile, records, metadata = parallel_install(install, count=2)
    refs = VerificationReferences()
    extracted, requested = [], []
    def fetch(url, session):
        requested.append(url)
        return metadata(url, session)
    def extract(archive, stage, **_):
        extracted.append(archive.name)
        (stage / "Option/gamedata").mkdir(parents=True)
        (stage / "Option/gamedata/a.script").write_bytes(b"original")
    def scan():
        return verify_sources(profile, records, None, threading.Event(), lambda _: None,
                              tree_fetch=lambda *_: {}, metadata_fetch=fetch,
                              extract=extract, references=refs)
    folder = next(iter(records))
    broken = Path(profile.gamma) / "mods" / folder / "gamedata/a.script"
    broken.write_bytes(b"damaged")
    assert scan().problems == 1
    broken.write_bytes(b"original")
    result = scan()
    assert result.complete and result.problems == 0
    assert sorted(extracted) == ["0.zip", "1.zip"]
    assert len(requested) == 2


def test_changed_cached_archive_is_checked_again(install):
    from commander_gui.source_integrity import VerificationReferences
    _, record, _, archive, run = install
    refs = VerificationReferences()
    assert run(references=refs).problems == 0
    archive.write_bytes(b"damaged archive")
    result = run(references=refs, extract=lambda *a, **k: pytest.fail("corrupt archive extracted"))
    assert result.archive_bad_mods == [record.folder_name]


def test_post_scan_rehashes_repaired_files_and_shared_patch_paths(install, monkeypatch):
    from commander_gui import source_integrity as si
    profile, record, mod, _, run = install
    refs = si.VerificationReferences()
    patch_file = mod.parent / "Core patches/gamedata/shared.script"
    patch_file.parent.mkdir(parents=True)
    patch_file.write_bytes(b"patch")
    def tree(url, _):
        if url == profile.gamma_setup_repo_url:
            return {"modpack_addons/Core patches/gamedata/shared.script": sha(b"patch")}
        return {}
    assert run(references=refs, tree_fetch=tree).problems == 0
    original = si.digest_file
    seen = []
    def digest(path, *args):
        seen.append(path)
        return original(path, *args)
    monkeypatch.setattr(si, "digest_file", digest)
    assert run(references=refs, tree_fetch=tree, repair_folders=[record.folder_name]).problems == 0
    assert patch_file in seen and mod / "gamedata/a.script" in seen
    assert all(path.suffix != ".zip" for path in seen)


def test_failed_archive_reference_is_not_reused_after_repair(install):
    from commander_gui.source_integrity import VerificationReferences
    _, record, _, archive, run = install
    refs = VerificationReferences()
    archive.write_bytes(b"broken")
    assert run(references=refs).archive_bad_mods == [record.folder_name]
    archive.write_bytes(b"verified archive")
    assert run(references=refs).problems == 0


def test_sparse_repair_catalogue_preserves_numbers_and_omits_healthy_mods(install):
    from dataclasses import replace

    from commander_gui.repair import parse_modpack_records, repair_catalogue
    _, record, *_ = install
    record = replace(record, counter=23)
    text = repair_catalogue({record.folder_name: record})
    assert len(text.splitlines()) == 23
    assert list(parse_modpack_records(text)) == [record.folder_name]
    assert repair_catalogue({}) == ""
    with pytest.raises(ValueError):
        repair_catalogue({"999- Wrong folder": record})


def test_repair_cloudflare_button_reaches_active_runner(tmp_path):
    import json
    from unittest.mock import Mock, patch

    from PySide6.QtWidgets import QApplication

    from commander_gui.moddb_session import ACCESS_PREFIX
    from commander_gui.repair import QuarantineRecord
    from commander_gui.settings import CliSettings
    from commander_gui.ui.install_page import InstallPage

    app = QApplication.instance() or QApplication([])
    window = Mock()
    window.settings = CliSettings(profiles=[CliProfile(active=True, gamma=str(tmp_path))])
    page = InstallPage(window)
    record = ModPackRecord(2, "Broken", "- Author", "https://www.moddb.com/addons/start/123", "", "a.zip", "a" * 32, "0")
    page._quarantine_records = [QuarantineRecord(record.folder_name)]
    page._repair_records = {record.folder_name: record}
    runner = Mock()
    runner.was_cancelled = False
    runner.verify_moddb.return_value = True
    with patch("commander_gui.ui.install_page.CommandRunner", return_value=runner), patch("commander_gui.ui.install_page.cli_command", side_effect=lambda args, **_: args) as command:
        page._start_repair_install()
        args = command.call_args.args[0]
        assert "--repair-only" in args
        catalogue = Path(args[args.index("--mod-pack-maker-path") + 1])
        assert catalogue.read_text().count("Broken") == 1
        panel = page.verify_progress.moddb_access
        panel.consume_line(ACCESS_PREFIX + json.dumps({"state": "required", "message": "Cookie required"}))
        assert panel.button.isEnabled()
        panel.button.click()
        runner.verify_moddb.assert_called_once()
        assert panel._state == "verifying"
    page._finish_verify(False, "Test finished", "Test")
    assert not catalogue.exists()
    assert not panel.button.isEnabled()
    page.deleteLater()
    app.processEvents()


def test_cloudflare_stale_request_gives_feedback():
    from PySide6.QtWidgets import QApplication

    from commander_gui.ui.moddb_access import ModDbAccessPanel
    app = QApplication.instance() or QApplication([])
    panel = ModDbAccessPanel(lambda: False)
    panel.start()
    panel._set_state("required", "Verify now")
    panel.button.click()
    assert not panel.button.isEnabled()
    assert "No ModDB request" in panel.instructions.text()
    panel.deleteLater()
    app.processEvents()


def test_repair_preserves_good_cached_archive_and_waits_to_discard_backup(install):
    from unittest.mock import Mock, patch

    from PySide6.QtWidgets import QApplication

    from commander_gui.integrity import GammaVerifyResult
    from commander_gui.settings import CliSettings
    from commander_gui.ui.install_page import InstallPage

    profile, record, mod, archive, _ = install
    profile.active = True
    app = QApplication.instance() or QApplication([])
    window = Mock()
    window.settings = CliSettings(profiles=[profile])
    page = InstallPage(window)
    page._source_scan = SourceScanResult(changed=[f"mods/{record.folder_name}/gamedata/a.script"])
    page._repair_records = {record.folder_name: record}
    page._repair_plan = classify_problems(page._source_scan, page._repair_records)
    page._quarantine_records = page._run_repair_quarantine(lambda _: None)
    assert archive.exists()
    assert not mod.exists()
    backup = page._quarantine_records[0].items[0].quarantined
    assert backup.exists()
    (mod / "gamedata").mkdir(parents=True)
    (mod / "gamedata/a.script").write_bytes(b"original")
    with patch.object(page, "_start_post_scan"):
        page._on_repair_install_finished(0, "Install finished")
    assert backup.exists()
    with patch.object(page, "_advance_repair_pipeline"):
        page._on_post_scan_done((SourceScanResult(), GammaVerifyResult()))
    assert not backup.exists()
    assert archive.exists()
    page.deleteLater()
    app.processEvents()


def test_completed_scan_clears_stale_cloudflare_prompt_before_repair_dialog(tmp_path):
    from unittest.mock import Mock, patch

    from PySide6.QtWidgets import QApplication

    from commander_gui.integrity import GammaVerifyResult
    from commander_gui.repair import RepairPlan
    from commander_gui.settings import CliSettings
    from commander_gui.ui.install_page import InstallPage

    app = QApplication.instance() or QApplication([])
    window = Mock()
    window.settings = CliSettings(profiles=[CliProfile(active=True, gamma=str(tmp_path))])
    page = InstallPage(window)
    panel = page.verify_progress.moddb_access
    panel.start()
    panel._set_state("required", "Cookie needed")
    def prompt(*_):
        assert not panel.button.isEnabled()
        assert "Session ended" in panel.status.text()
    with patch.object(page, "_prompt_repair", side_effect=prompt) as called:
        page._on_gamma_verify_done((GammaVerifyResult(), SourceScanResult(changed=["mods/Broken/a"]), RepairPlan(repairable=["Broken"]), {}, False, None))
        called.assert_called_once()
    page.deleteLater()
    app.processEvents()
