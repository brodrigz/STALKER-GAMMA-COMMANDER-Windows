"""Existing Grok installer layouts and MO2 profile recovery."""

import io
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from commander_gui import updates
from commander_gui.install_layout import (
    WINDOWS_INSTALLER,
    mo2_profiles,
    resolve_mo2_profile,
)
from commander_gui.settings import CliProfile, load_settings

CATALOGUE = "https://mods/1\t\t- Author\tSound\thttps://mods/sound\tnew.zip\tnew-md5\n"


def windows_install(root):
    installer = root / WINDOWS_INSTALLER
    installer.mkdir()
    (installer / "version.txt").write_text("920\n", encoding="utf-8-sig")
    (installer / "mods.txt").write_text(CATALOGUE, encoding="utf-8-sig")
    mod = root / "mods/1- Sound - Author"
    mod.mkdir(parents=True)
    (mod / "meta.ini").write_text('[General]\ninstallationFile=C:\\Downloads\\old.zip\n', encoding="utf-8-sig")
    return installer, mod


def profile(root, name, entries):
    path = root / "profiles" / name
    path.mkdir(parents=True)
    (path / "modlist.txt").write_text("# MO2\n" + entries, encoding="utf-8-sig")
    return path


def test_windows_metadata_uses_actual_installed_archive_and_build(tmp_path):
    windows_install(tmp_path)
    records = updates.local_modpack_records(str(tmp_path), "GAMMA Custom")
    assert updates.installed_version(str(tmp_path)) == "920"
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record.zip_name == "old.zip"
    assert not record.checksum_known and not record.md5_mod_db
    diffs = updates.diff_records(records, updates.parse_modpack_records(CATALOGUE))
    assert len(diffs) == 1 and diffs[0].status == "Modified"


def test_cached_catalogue_does_not_invent_installed_hashes(tmp_path):
    windows_install(tmp_path)
    local = updates.local_modpack_records(str(tmp_path), "GAMMA Custom")
    remote = {name: replace(record, md5_mod_db="different", checksum_known=True) for name, record in local.items()}
    diffs = updates.diff_records(local, remote)
    assert len(diffs) == 1 and "checksum unknown" in diffs[0].detail


def test_missing_installed_archive_is_not_compared_to_cached_archive(tmp_path):
    _, mod = windows_install(tmp_path)
    (mod / "meta.ini").unlink()
    local = updates.local_modpack_records(str(tmp_path), "GAMMA Custom")
    diffs = updates.diff_records(local, updates.parse_modpack_records(CATALOGUE))
    assert len(diffs) == 1 and "checksum unknown" in diffs[0].detail


def test_cli_snapshot_and_build_take_precedence(tmp_path):
    windows_install(tmp_path)
    directory = profile(tmp_path, "G.A.M.M.A", "+Sound\n")
    (directory / "modpack_maker_list.txt").write_text(CATALOGUE, encoding="utf-8-sig")
    (tmp_path / "version.txt").write_text("930")
    assert updates.installed_version(str(tmp_path)) == "930"
    assert next(iter(updates.local_modpack_records(str(tmp_path), "G.A.M.M.A").values())).checksum_known


def test_windows_repository_catalogue_fallback(tmp_path):
    installer, _ = windows_install(tmp_path)
    (installer / "mods.txt").unlink()
    data = installer / "G.A.M.M.A/modpack_data"
    data.mkdir(parents=True)
    (data / "modpack_maker_list.txt").write_text(CATALOGUE)
    assert updates.local_modpack_records(str(tmp_path), "GAMMA Custom")


def test_missing_metadata_still_fetches_version_and_patchnotes(monkeypatch, tmp_path):
    installer = tmp_path / WINDOWS_INSTALLER
    installer.mkdir()
    (installer / "version.txt").write_text("910")
    monkeypatch.setattr(updates, "remote_version", lambda profile: "920")
    monkeypatch.setattr(updates, "fetch_latest_patchnotes", lambda profile: "Release notes")
    monkeypatch.setattr(updates, "_version_from_patchnotes_or_readme", lambda *args: "0.9.5")
    result = updates.check_updates(CliProfile(gamma=str(tmp_path)))
    assert result.update_available and result.latest == "920"
    assert result.patchnotes == "Release notes"
    assert "full install" not in result.error


def test_windows_update_check_reports_archive_change(monkeypatch, tmp_path):
    windows_install(tmp_path)
    monkeypatch.setattr(updates, "remote_version", lambda profile: "920")
    monkeypatch.setattr(updates, "fetch_latest_patchnotes", lambda profile: "Release notes")
    monkeypatch.setattr(updates, "_version_from_patchnotes_or_readme", lambda *args: "0.9.5")
    response = io.BytesIO(CATALOGUE.encode())
    response.headers = {}
    monkeypatch.setattr(updates, "urlopen", lambda *args, **kwargs: response)
    result = updates.check_updates(CliProfile(gamma=str(tmp_path)))
    assert result.error is None and result.update_available
    assert result.installed == "920"
    assert "refreshed" in result.note


def test_import_creates_cli_compatible_snapshot_without_claiming_hashes(tmp_path):
    from commander_gui.cli_import import prepare_update_snapshot

    windows_install(tmp_path)
    directory = profile(tmp_path, "GAMMA Custom", "+1- Sound - Author\n")
    configured = CliProfile(gamma=str(tmp_path), mo2_profile="GAMMA Custom")
    assert "Imported 1" in prepare_update_snapshot(configured)
    path = directory / "modpack_maker_list.json"
    data = json.loads(path.read_text())
    assert data[0]["instructions"] == []
    assert data[0]["patch"] == "Author"
    assert data[0]["zipName"] == "old.zip"
    assert data[0]["md5ModDb"] is None
    assert data[0]["commanderChecksumKnown"] is False
    before = path.read_bytes()
    assert prepare_update_snapshot(configured) is None
    assert path.read_bytes() == before
    record = next(iter(updates.local_modpack_records(str(tmp_path), "GAMMA Custom").values()))
    assert not record.checksum_known and record.patch == "- Author"


def test_import_refuses_to_overwrite_invalid_existing_snapshot(tmp_path):
    import pytest

    from commander_gui.cli_import import prepare_update_snapshot

    directory = profile(tmp_path, "GAMMA Custom", "+Sound\n")
    path = directory / "modpack_maker_list.json"
    path.write_text("not json")
    with pytest.raises(ValueError, match="Cannot read"):
        prepare_update_snapshot(CliProfile(gamma=str(tmp_path), mo2_profile="GAMMA Custom"))
    assert path.read_text() == "not json"


def test_recovers_only_unambiguous_empty_default(tmp_path):
    windows_install(tmp_path)
    profile(tmp_path, "G.A.M.M.A", "+Audio_separator\n")
    profile(tmp_path, "GAMMA Custom", "+Sound\n-Other\n")
    assert mo2_profiles(tmp_path) == {"G.A.M.M.A": 0, "GAMMA Custom": 2}
    assert resolve_mo2_profile(tmp_path, "G.A.M.M.A") == "GAMMA Custom"
    assert resolve_mo2_profile(tmp_path, "My empty profile") == "My empty profile"
    profile(tmp_path, "Another", "+Sound\n")
    assert resolve_mo2_profile(tmp_path, "G.A.M.M.A") == "G.A.M.M.A"


def test_settings_recovery_persists_for_cli_and_preserves_unknown_keys(tmp_path):
    windows_install(tmp_path)
    profile(tmp_path, "G.A.M.M.A", "")
    profile(tmp_path, "GAMMA Custom", "+Sound\n")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"extra": 42, "Profiles": [{"Active": True, "Gamma": str(tmp_path), "Mo2Profile": "G.A.M.M.A", "future": 5}]}))
    settings = load_settings(path)
    assert settings.active_profile.mo2_profile == "GAMMA Custom"
    saved = json.loads(path.read_text())
    assert saved["extra"] == 42 and saved["Profiles"][0]["future"] == 5
    assert saved["Profiles"][0]["Mo2Profile"] == "GAMMA Custom"


@pytest.mark.parametrize("url, migrate", [
    ("https://stalker-gamma.com/api/client/v1/mods/list", True),
    ("https://STALKER-GAMMA.COM/api/list/", True),
    ("https://custom.example/mods", False),
    ("https://stalker-gamma.com/custom-list", False),
])
def test_catalogue_migration_keeps_gui_and_cli_consistent(tmp_path, url, migrate):
    from commander_gui.settings import DEFAULT_MOD_PACK_MAKER_URL

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"Profiles": [{"Active": True, "ModPackMakerUrl": url}]}))
    settings = load_settings(path)
    expected = DEFAULT_MOD_PACK_MAKER_URL if migrate else url
    assert settings.active_profile.mod_pack_maker_url == expected
    assert json.loads(path.read_text())["Profiles"][0]["ModPackMakerUrl"] == expected


@pytest.mark.parametrize("valid", [True, False])
def test_update_apply_prepares_snapshot_before_starting_cli(monkeypatch, tmp_path, valid):
    from commander_gui.ui import update_page

    windows_install(tmp_path)
    directory = profile(tmp_path, "GAMMA Custom", "+1- Sound - Author\n-Custom\n")
    snapshot = directory / "modpack_maker_list.json"
    if not valid:
        snapshot.write_text("broken")
    active = CliProfile(gamma=str(tmp_path), cache=str(tmp_path), mo2_profile="GAMMA Custom")
    page = SimpleNamespace(
        _applying=False, _checking=False, _diffs=[object()],
        window=SimpleNamespace(install_busy=False, settings=SimpleNamespace(active_profile=active), set_install_busy=Mock()),
        _snapshot_modlist_before_update=Mock(), minimal_cb=Mock(), preserve_user_cb=Mock(), preserve_mcm_cb=Mock(),
        apply_progress=Mock(), _on_apply_finished=Mock(),
    )
    monkeypatch.setattr(update_page.QMessageBox, "question", lambda *args: update_page.QMessageBox.StandardButton.Yes)
    warning = Mock()
    monkeypatch.setattr(update_page.QMessageBox, "warning", warning)
    monkeypatch.setattr(update_page, "free_space_bytes", lambda *args: None)
    monkeypatch.setattr(update_page, "mo2_running", lambda **kwargs: False)
    backup = Mock(return_value=None)
    monkeypatch.setattr(update_page, "backup_settings_before", backup)
    runner = Mock()

    def create_runner(*args, **kwargs):
        assert json.loads(snapshot.read_text())[0]["commanderChecksumKnown"] is False
        return runner

    factory = Mock(side_effect=create_runner)
    monkeypatch.setattr(update_page, "CommandRunner", factory)
    update_page.UpdatePage._apply(page)
    if valid:
        runner.start.assert_called_once()
        backup.assert_called_once()
        warning.assert_not_called()
    else:
        factory.assert_not_called()
        backup.assert_not_called()
        warning.assert_called_once()
        assert snapshot.read_text() == "broken"


def test_profiles_picker_lists_existing_profiles(tmp_path):
    from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

    from commander_gui.ui.profiles_page import ProfilesPage

    app = QApplication.instance() or QApplication([])
    windows_install(tmp_path)
    profile(tmp_path, "G.A.M.M.A", "")
    profile(tmp_path, "GAMMA Custom", "+Sound\n")
    combo = QComboBox()
    combo.setEditable(True)
    combo.setEditText("G.A.M.M.A")
    page = SimpleNamespace(gamma_edit=QLineEdit(str(tmp_path)), mo2_edit=combo.lineEdit(), mo2_combo=combo)
    ProfilesPage._refresh_mo2_profiles(page)
    assert combo.currentText() == "GAMMA Custom"
    assert {combo.itemText(i) for i in range(combo.count())} == {"G.A.M.M.A", "GAMMA Custom"}
    assert app is not None
