import hashlib
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from commander_gui.coop import (
    MOD,
    PROFILE,
    CoopError,
    CoopManager,
    compare_manifests,
    safe_path,
)
from commander_gui.settings import CliProfile


def put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


@pytest.fixture
def setup(tmp_path):
    gamma, anomaly, package = (tmp_path / name for name in ("gamma", "anomaly", "package"))
    put(gamma, "ModOrganizer.ini", f"[General]\ngamePath=@ByteArray({anomaly})\n")
    put(gamma, "profiles/G.A.M.M.A/modlist.txt", "+Custom\n-Optional\n+Base\n")
    put(gamma, "profiles/G.A.M.M.A/settings.ini", "[General]\nLocalSaves=false\nLocalSettings=true\n")
    put(gamma, "profiles/G.A.M.M.A/user.ltx", "renderer renderer_r4\nxrr_player_name Old\n")
    put(gamma, "profiles/G.A.M.M.A/saves/solo.scop", b"save")
    put(gamma, "mods/Custom/gamedata/configs/a.ltx", "custom")
    put(gamma, "mods/Custom/gamedata/configs/axr_options.ltx", "[mcm]\ncustom/value=12\n")
    put(gamma, "mods/Base/gamedata/configs/a.ltx", "base")
    put(anomaly, "bin/AnomalyDX11.exe", b"original engine")
    put(anomaly, "gamedata/configs/system.ltx", b"original config")
    put(package, "Anomaly/bin/AnomalyDX11.exe", b"coop engine")
    put(package, "Anomaly/bin/new.dll", b"coop dll")
    put(package, "Anomaly/gamedata/configs/system.ltx", b"coop engine config")
    put(package, "gamedata/scripts/xrr.script", b"coop script")
    profile = CliProfile(gamma=str(gamma), anomaly=str(anomaly), mo2_profile="G.A.M.M.A")
    manager = CoopManager(profile, storage=tmp_path / "store", running=lambda: False)
    return manager, package


def test_install_clones_without_saves_and_switches_reversibly(setup):
    m, package = setup
    before = (m.gamma / "profiles/G.A.M.M.A/modlist.txt").read_bytes()
    m.install_payload(package)
    assert (m.gamma / "profiles/G.A.M.M.A/modlist.txt").read_bytes() == before
    assert (m.gamma / f"profiles/{PROFILE}/modlist.txt").read_bytes() == b"+xrRazom Co-op\n" + before
    assert not (m.gamma / f"profiles/{PROFILE}/saves/solo.scop").exists()
    assert "LocalSaves=true" in (m.gamma / f"profiles/{PROFILE}/settings.ini").read_text()
    assert (m.anomaly / "bin/AnomalyDX11.exe").read_bytes() == b"coop engine"
    assert m.switch(False) == "G.A.M.M.A"
    assert (m.anomaly / "bin/AnomalyDX11.exe").read_bytes() == b"original engine"
    assert not (m.anomaly / "bin/new.dll").exists()
    assert m.switch(True) == PROFILE
    assert (m.anomaly / "bin/new.dll").read_bytes() == b"coop dll"


@pytest.mark.parametrize("cached", [False, True])
def test_zip_install_streams_extraction_to_ui(setup, tmp_path, monkeypatch, cached):
    from PySide6.QtWidgets import QApplication

    from commander_gui.coop import SLIM_VERSION
    from commander_gui.mod_install import ModInstallError, find_archiver
    from commander_gui.ui.common import _StreamWorker

    try:
        find_archiver()
    except ModInstallError as exc:
        pytest.skip(str(exc))
    app = QApplication.instance() or QApplication([])
    m, package = setup
    cache = m.root / "downloads" / f"xrRazom-{SLIM_VERSION}-Slim.zip"
    archive = cache if cached else tmp_path / "selected.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as output:
        for path in package.rglob("*"):
            if path.is_file():
                output.write(path, path.relative_to(package).as_posix())
    monkeypatch.setattr("commander_gui.coop.SLIM_MD5", hashlib.md5(archive.read_bytes()).hexdigest())
    worker = _StreamWorker(lambda report: m.install_zip(archive, report=report))
    lines, results, errors = [], [], []
    worker.line.connect(lines.append)
    worker.result.connect(results.append)
    worker.error.connect(errors.append)
    worker.run()
    assert errors == []
    assert len(results) == 1
    assert any("Everything is Ok" in line for line in lines)
    assert m.state()["installed"]
    assert cache.read_bytes() == archive.read_bytes()
    assert (m.gamma / f"mods/{MOD}/gamedata/scripts/xrr.script").read_bytes() == b"coop script"
    m.switch(False)
    assert (m.anomaly / "bin/AnomalyDX11.exe").read_bytes() == b"original engine"
    worker.deleteLater()
    app.processEvents()


def test_play_preview_does_not_hash_engine_or_disable_valid_coop_launch(setup, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from commander_gui.launcher import LaunchError, Mo2Executable, Runner
    from commander_gui.ui.play_page import PlayPage

    app = QApplication.instance() or QApplication([])
    m, package = setup
    m.install_payload(package)
    m.profile.mo2_profile = PROFILE
    put(m.gamma, "ModOrganizer.exe", b"fixture")
    exe = Mo2Executable(title="DX11", binary=str(m.anomaly / "bin/AnomalyDX11.exe"))
    monkeypatch.setattr("commander_gui.coop.CoopManager", lambda _: m)
    monkeypatch.setattr("commander_gui.ui.play_page.parse_mo2_executables", lambda _: [exe])
    monkeypatch.setattr(PlayPage, "_fetch_proton_releases", lambda _: None)
    monkeypatch.setattr(PlayPage, "_reload_runners", lambda _: None)
    monkeypatch.setattr(PlayPage, "_runner", lambda _: Runner("native", "Windows", [], {}))
    hashes = Mock(wraps=m._engine_changes)
    monkeypatch.setattr(m, "_engine_changes", hashes)
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=m.profile), refresh_settings=lambda: None)
    page = PlayPage(window)
    try:
        page.refresh()
        page._refresh_preview()
        hashes.assert_not_called()
        assert page.launch_button.isEnabled()
        assert page.open_mo2_button.isEnabled()
        assert not page.direct_button.isEnabled()
        assert "through Mod Organizer" in page.direct_button.toolTip()
        assert page.launch_notice.isHidden()

        # Full validation is still mandatory for the actual launch command.
        put(m.anomaly, "bin/AnomalyDX11.exe", b"damaged")
        with pytest.raises(LaunchError, match="Engine file changed"):
            page._resolve_command(open_mo2=False, direct=False)
        hashes.assert_called_once()

        # A genuine mode mismatch remains blocked, with a visible explanation.
        m.profile.mo2_profile = "G.A.M.M.A"
        page._refresh_preview()
        assert not page.launch_button.isEnabled()
        assert not page.launch_notice.isHidden()
        assert "Profile and engine differ" in page.launch_notice.text()
        m.profile.mo2_profile = PROFILE
        page._refresh_preview()
        assert page.launch_button.isEnabled()
        assert page.launch_notice.isHidden()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_second_install_keeps_original_backups_and_profile_changes(setup):
    m, package = setup
    m.install_payload(package)
    put(m.gamma, f"profiles/{PROFILE}/modlist.txt", "+Custom\n-Base\n")
    m.install_payload(package)
    assert "-Base" in (m.gamma / f"profiles/{PROFILE}/modlist.txt").read_text()
    m.switch(False)
    assert (m.anomaly / "bin/AnomalyDX11.exe").read_bytes() == b"original engine"


def test_transaction_failure_restores_every_changed_file(setup, monkeypatch):
    m, package = setup
    original = m._put
    calls = 0
    def fail_once(key, value):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("disk full")
        original(key, value)
    monkeypatch.setattr(m, "_put", fail_once)
    with pytest.raises(OSError, match="disk full"):
        m.install_payload(package)
    assert not m.state()
    assert not m.journal_path.exists()
    assert (m.anomaly / "bin/AnomalyDX11.exe").read_bytes() == b"original engine"
    assert not (m.gamma / f"profiles/{PROFILE}/modlist.txt").exists()


def test_recovery_after_process_crash(setup, monkeypatch):
    m, package = setup
    original = m._put
    def crash(key, value):
        original(key, value)
        raise KeyboardInterrupt()
    monkeypatch.setattr(m, "_put", crash)
    with pytest.raises(KeyboardInterrupt):
        m.install_payload(package)
    assert m.journal_path.exists()
    monkeypatch.setattr(m, "_put", original)
    with pytest.raises(CoopError, match="recovery"):
        m.switch(False)
    m.recover()
    assert not m.journal_path.exists()
    assert not (m.gamma / f"profiles/{PROFILE}/modlist.txt").exists()


def test_recovery_does_not_overwrite_external_edits(setup, monkeypatch):
    m, package = setup
    def crash(key, value):
        raise KeyboardInterrupt()
    monkeypatch.setattr(m, "_put", crash)
    with pytest.raises(KeyboardInterrupt):
        m.install_payload(package)
    put(m.gamma, f"profiles/{PROFILE}/modlist.txt", "manual edit")
    with pytest.raises(CoopError, match="changed outside"):
        m.recover()
    assert (m.gamma / f"profiles/{PROFILE}/modlist.txt").read_text() == "manual edit"


def test_update_rebases_backups_and_preserves_custom_order(setup):
    m, package = setup
    m.install_payload(package)
    put(m.gamma, f"profiles/{PROFILE}/modlist.txt", "+xrRazom Co-op\n-Custom\n+Base\n")
    m.begin_maintenance()
    assert not m.state()["active"]
    put(m.anomaly, "bin/AnomalyDX11.exe", "updated GAMMA engine")
    put(m.gamma, f"profiles/{PROFILE}/modlist.txt", "+New Mod\n+Base\n+Custom\n")
    m.finish_maintenance(True)
    assert m.state()["active"]
    assert (m.gamma / f"profiles/{PROFILE}/modlist.txt").read_text().startswith("+xrRazom Co-op\n+New Mod\n")
    assert "-Custom" in (m.gamma / f"profiles/{PROFILE}/modlist.txt").read_text()
    m.switch(False)
    assert (m.anomaly / "bin/AnomalyDX11.exe").read_text() == "updated GAMMA engine"


def test_failed_update_blocks_launch_until_successful_retry(setup):
    m, package = setup
    m.install_payload(package)
    m.begin_maintenance()
    m.finish_maintenance(False)
    with pytest.raises(CoopError, match="repair"):
        m.assert_launch()
    assert m.begin_maintenance()
    m.finish_maintenance(True)
    assert not m.state()["maintenance"]


def test_damage_can_be_repaired_but_not_silently_used(setup):
    m, package = setup
    m.install_payload(package)
    m.profile.mo2_profile = PROFILE
    put(m.anomaly, "bin/AnomalyDX11.exe", "broken")
    with pytest.raises(CoopError, match="changed outside"):
        m.assert_launch()
    m.begin_maintenance()
    m.finish_maintenance(True)
    m.assert_launch()


def test_adoption_requires_explicit_choice_and_preserves_profile(setup):
    m, package = setup
    put(m.gamma, f"profiles/{PROFILE}/modlist.txt", "-Custom\n+Base\n")
    with pytest.raises(CoopError, match="already exists"):
        m.install_payload(package)
    m.install_payload(package, adopt=True)
    assert "-Custom" in (m.gamma / f"profiles/{PROFILE}/modlist.txt").read_text()


def test_rejects_manual_coop_engine_as_singleplayer_backup(setup):
    m, package = setup
    put(m.anomaly, "bin/AnomalyDX11.exe", "coop engine")
    with pytest.raises(CoopError, match="Restore the GAMMA engines"):
        m.install_payload(package, adopt=True)
    assert not m.state()


def test_options_are_isolated_and_identity_persists(setup):
    m, package = setup
    m.install_payload(package)
    before = (m.gamma / "profiles/G.A.M.M.A/user.ltx").read_bytes()
    m.save_options("Stalker", True, False, 5445, 4, "192.168.1.2")
    text = (m.gamma / f"profiles/{PROFILE}/user.ltx").read_text()
    assert "xrr_player_name Stalker\n" in text
    assert "xrr_player_name Old" not in text
    assert "renderer renderer_r4" in text
    cfg = (m.gamma / f"mods/{MOD}/gamedata/configs/axr_options.ltx").read_text()
    assert "custom/value=12" in cfg
    assert "settings_migrated=true" in cfg
    assert (m.gamma / "profiles/G.A.M.M.A/user.ltx").read_bytes() == before
    m.install_payload(package)
    assert m.state()["options"]["name"] == "Stalker"
    assert "xrr_player_name Stalker" in (m.gamma / f"profiles/{PROFILE}/user.ltx").read_text()


def test_uninstall_restores_engine_keeps_saves(setup):
    m, package = setup
    m.install_payload(package)
    put(m.gamma, f"profiles/{PROFILE}/saves/coop.scop", "coop save")
    assert m.uninstall() == "G.A.M.M.A"
    assert (m.gamma / f"profiles/{PROFILE}/saves/coop.scop").read_text() == "coop save"
    assert (m.anomaly / "bin/AnomalyDX11.exe").read_bytes() == b"original engine"
    assert not (m.gamma / f"mods/{MOD}/gamedata/scripts/xrr.script").exists()
    assert not m.state()["installed"]


def test_manifest_compares_winning_configs_without_personal_data(setup):
    m, package = setup
    m.install_payload(package)
    manifest = m.manifest()
    assert str(m.gamma) not in json.dumps(manifest)
    assert not any("axr_options" in name for name in manifest["configs"])
    assert compare_manifests(manifest, manifest) == []
    put(m.gamma, "mods/Custom/gamedata/configs/a.ltx", "new config")
    assert compare_manifests(m.manifest(), manifest) == ["Config differs: gamedata/configs/a.ltx"]


def test_reference_store_not_installed_files_is_authoritative(setup):
    m, package = setup
    m.install_payload(package)
    expected = m.expected_files()
    put(m.anomaly, "bin/AnomalyDX11.exe", "corrupt")
    assert m.expected_files() == expected
    key = m.state()["payload"]["game/bin/AnomalyDX11.exe"]
    put(m.root, "objects/" + key, "corrupt backup")
    with pytest.raises(CoopError, match="backup is damaged"):
        m.expected_files()


@pytest.mark.parametrize("path", ["../outside", "bin/../../outside", "C:/outside", "bin/file:stream"])
def test_rejects_unsafe_paths(tmp_path, path):
    with pytest.raises(CoopError):
        safe_path(tmp_path, path)


def test_wont_mutate_running_game(setup):
    m, package = setup
    m.running = lambda: True
    with pytest.raises(CoopError, match="Close Anomaly"):
        m.install_payload(package)
    assert not m.state()


def test_refuses_wrong_gamepath_and_unverified_archive(setup, tmp_path):
    m, package = setup
    put(m.gamma, "ModOrganizer.ini", "gamePath=C:/wrong\n")
    with pytest.raises(CoopError, match="differs"):
        m.install_payload(package)
    archive = put(tmp_path, "bad.zip", b"not an official archive")
    with pytest.raises(CoopError, match="publisher MD5"):
        m.install_zip(archive)


def test_options_prefer_changes_made_in_game(setup):
    m, package = setup
    m.install_payload(package)
    m.save_options("First", False, True, 5445, 4, "")
    put(m.gamma, f"profiles/{PROFILE}/user.ltx", "xrr_player_name Changed in game\nxrr_host_session on\n")
    assert m.options()["name"] == "Changed in game"
    assert m.options()["host"] is True


def test_coop_repair_restores_payload_without_touching_custom_mods(setup):
    m, package = setup
    m.install_payload(package)
    script = put(m.gamma, f"mods/{MOD}/gamedata/scripts/xrr.script", "damaged")
    custom = put(m.gamma, "mods/Custom/extra.txt", "keep")
    m.repair()
    assert script.read_bytes() == b"coop script"
    assert custom.read_text() == "keep"


def test_source_verification_uses_coop_engine_reference(setup, monkeypatch):
    import threading

    from commander_gui.source_integrity import verify_sources
    m, package = setup
    m.install_payload(package)
    monkeypatch.setattr("commander_gui.coop.CoopManager", lambda _: m)
    args = (m.profile, {}, None, threading.Event(), lambda _: None)
    clean = verify_sources(*args, tree_fetch=lambda *_: {})
    assert not clean.anomaly_changed
    put(m.anomaly, "bin/AnomalyDX11.exe", "damaged")
    broken = verify_sources(*args, tree_fetch=lambda *_: {})
    assert broken.anomaly_changed == ["Anomaly/bin/AnomalyDX11.exe"]


def test_coop_vanilla_mismatch_is_deferred_to_source_check(setup):
    from commander_gui.integrity import is_expected_gamma_overlay_corrupt
    m, _ = setup
    line = str(m.anomaly / "bin/new.dll") + " | CORRUPT"
    assert not is_expected_gamma_overlay_corrupt(line, str(m.anomaly))
    assert is_expected_gamma_overlay_corrupt(line, str(m.anomaly), extra_files={"bin/new.dll"})


def test_custom_coop_profile_is_used_through_install_switch_repair_and_remove(setup):
    manager, package = setup
    manager.install_payload(package, coop_profile="Friends campaign")
    assert manager.profile.mo2_coop_profile == "Friends campaign"
    assert manager.profile.singleplayer_profile == "G.A.M.M.A"
    assert manager.state()["coop_profile"] == "Friends campaign"
    assert not (manager.gamma / "profiles" / PROFILE).exists()
    manager.profile.select_mo2_profile("Friends campaign")
    manager.assert_launch()
    assert manager.switch(False) == "G.A.M.M.A"
    manager.profile.select_mo2_profile("G.A.M.M.A")
    assert manager.switch(True) == "Friends campaign"
    manager.profile.select_mo2_profile("Friends campaign")
    assert manager.begin_maintenance()
    manager.finish_maintenance(True)
    manager.repair()
    manager.assert_launch()
    assert manager.uninstall() == "G.A.M.M.A"
    assert (manager.gamma / "profiles/Friends campaign/modlist.txt").is_file()
    assert manager.profile.singleplayer_profile == "G.A.M.M.A"


@pytest.mark.parametrize("name", ["", "Missing", "G.A.M.M.A", "../escape"])
def test_invalid_configured_coop_profile_blocks_launch(setup, name):
    manager, package = setup
    manager.install_payload(package)
    manager.profile.select_mo2_profile(PROFILE)
    manager.profile.mo2_coop_profile = name
    with pytest.raises(CoopError):
        manager.assert_launch(verify_files=False)


def test_missing_or_disabled_coop_modlist_blocks_launch(setup):
    manager, package = setup
    manager.install_payload(package)
    manager.profile.select_mo2_profile(PROFILE)
    path = manager.gamma / "profiles" / PROFILE / "modlist.txt"
    path.write_text("-xrRazom Co-op\n+Base\n")
    with pytest.raises(CoopError, match="highest priority"):
        manager.assert_launch(verify_files=False)
    path.unlink()
    with pytest.raises(CoopError, match="missing or invalid"):
        manager.assert_launch(verify_files=False)


@pytest.mark.parametrize("name", ["../escape", "CON", "Bad/Profile", "NUL.txt", "Trailing.", "G.A.M.M.A"])
def test_install_rejects_unsafe_or_singleplayer_coop_name_before_changes(setup, name):
    manager, package = setup
    with pytest.raises(CoopError):
        manager.install_payload(package, coop_profile=name)
    assert (manager.anomaly / "bin/AnomalyDX11.exe").read_bytes() == b"original engine"
    assert not manager.state().get("installed")
