import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from commander_gui.coop import PROFILE
from commander_gui.moddb_session import ACCESS_PREFIX
from commander_gui.settings import CliProfile
from commander_gui.ui.coop_page import CoopPage


@pytest.fixture
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def test_coop_update_check_and_changelog_survive_network_failure(app, tmp_path, monkeypatch):
    from commander_gui.coop_updates import CoopRelease

    tasks = []

    class Task(QObject):
        result = Signal(object)
        error = Signal(str)

        def __init__(self, fn, parent=None):
            super().__init__(parent)
            self.start = Mock()
            self.cancel = Mock()
            tasks.append(self)

    monkeypatch.setattr("commander_gui.ui.coop_page.BackgroundTask", Task)
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False)
    page = CoopPage(window)
    try:
        page.refresh()
        assert not tasks  # Tab refresh must not generate repeated ModDB requests.
        page._installed_version = "1.3"
        page.check_updates_button.click()
        page._check_updates()
        assert len(tasks) == 1
        assert not page.check_updates_button.isEnabled()
        assert "Checking" in page.release_status.text()
        release = CoopRelease("1.4", "xrRazom v1.4", "Release summary", "2026-10-03",
                              "https://www.moddb.com/mods/xrrazom-stalker-anomaly-co-op/news/release")
        tasks[0].result.emit([release])
        assert page.check_updates_button.isEnabled()
        assert "Update available" in page.release_status.text()
        assert "upgrade instructions" in page.release_status.text()
        section = page.notes_layout.itemAt(0).widget()
        assert section.body.toPlainText() == "Release summary"
        assert section.toggle_button.isChecked()
        section.toggle_button.click()
        assert section.body.isHidden()
        assert section.layout().itemAt(2).widget().isHidden()
        checked = page.last_checked.text()
        page.check_updates_button.click()
        tasks[1].error.emit("Connection timed out")
        assert page.check_updates_button.isEnabled()
        assert "Update check failed" in page.release_status.text()
        assert page.notes_layout.itemAt(0).widget() is section
        assert page.last_checked.text() == checked
        page.check_updates_button.click()
        tasks[2].result.emit([release])
        assert "failed" not in page.release_status.text()
        page._installed_version = "1.4.0"
        page._render_release_status()
        assert "latest announced" in page.release_status.text()
        page._releases_loaded([CoopRelease("1.5", "xrRazom v1.5", "New", "", release.url)])
        assert "Commander currently installs Slim 1.4" in page.release_status.text()
        assert "Download and install" not in page.release_status.text()
    finally:
        page.close()
        page.deleteLater()


def test_page_disables_actions_during_work(app, tmp_path, monkeypatch):
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False)
    page = CoopPage(window)
    try:
        assert "Not installed" in page.status.text()
        page.on_busy_changed(True)
        assert all(not b.isEnabled() for b in page.buttons)
        page.on_busy_changed(False)
        assert all(b.isEnabled() for b in page.buttons if b is not page.play_button)
        assert not page.play_button.isEnabled()
        assert page.minimumSizeHint().height() < 500  # Scrolls at laptop sizes.
    finally:
        page.close()
        page.deleteLater()


def test_cancel_download_keeps_busy_until_process_finishes(app, tmp_path):
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=True)
    page = CoopPage(window)
    cancelled = []
    page.runner = SimpleNamespace(cancel=lambda: cancelled.append(True))
    try:
        page._cancel()
        assert cancelled == [True]
        assert window.install_busy
        assert not page.progress.cancel_button.isEnabled()
    finally:
        page.close()
        page.deleteLater()


def test_cli_worker_restores_coop_before_emitting_finished(app):
    from commander_gui.cli_runner import CliWorker
    worker = CliWorker()
    events = []
    worker._coop_maintenance = SimpleNamespace(finish_maintenance=lambda success: events.append(("restore", success)))
    worker._coop_lease = SimpleNamespace(unlock=lambda: events.append("unlock"))
    worker.finished.connect(lambda rc, output: events.append(("finished", rc)))
    worker._finish(0, "Completed")
    assert events == [("restore", True), "unlock", ("finished", 0)]


def test_cli_worker_reports_restore_failure_as_failure(app):
    from commander_gui.cli_runner import CliWorker
    worker = CliWorker()
    results = []
    def fail(_):
        raise OSError("disk unavailable")
    worker._coop_maintenance = SimpleNamespace(finish_maintenance=fail)
    worker.finished.connect(lambda rc, output: results.append((rc, output)))
    worker._finish(0, "Completed")
    assert results[0][0] != 0
    assert "disk unavailable" in results[0][1]


def test_coop_download_exposes_verification_while_busy(app, tmp_path, monkeypatch):
    class Runner(QObject):
        line = Signal(str)
        finished = Signal(int, str)

        def __init__(self, args, parent=None):
            super().__init__(parent)
            self.args = args
            self.start = Mock()
            self.verify_moddb = Mock(return_value=True)
            self.was_cancelled = False

    monkeypatch.setattr("commander_gui.ui.coop_page.CommandRunner", Runner)
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False)
    page = CoopPage(window)

    def busy(value, **kwargs):
        window.install_busy = value
        page.on_busy_changed(value)

    window.set_install_busy = busy
    install = Mock()
    monkeypatch.setattr(page, "_install", install)
    monkeypatch.setattr(page, "_prepare_coop_profile", lambda: True)
    try:
        page.show()
        page._download()
        runner = page.runner
        assert runner.args[1:3] == ["addon", "download"]
        assert window.install_busy
        runner.start.assert_called_once()
        panel = page.progress.moddb_access
        assert panel.isHidden()
        runner.line.emit(ACCESS_PREFIX + json.dumps({"state": "required", "message": "HTTP 403. Verify to continue."}))
        app.processEvents()
        assert panel.isVisible() and panel.button.isEnabled()
        assert all(not b.isEnabled() for b in page.buttons)
        assert page.progress.log.isHidden()
        runner.verify_moddb.assert_not_called()
        panel.button.click()
        runner.verify_moddb.assert_called_once()
        assert panel._state == "verifying"
        runner.line.emit(ACCESS_PREFIX + json.dumps({"state": "ready", "message": "Cookie accepted"}))
        runner.finished.emit(0, "Completed")
        assert panel.isHidden()
        install.assert_called_once()
        assert install.call_args.kwargs == {"already_busy": True}
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


# Retained alongside the commented session UI, for when that integration is validated.
# @pytest.fixture
# def session_page(app, tmp_path, monkeypatch):
#     monkeypatch.setattr("commander_gui.ui.coop_page.SESSION_SETTINGS_ENABLED", True)
#     window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
#         gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False)
#     page = CoopPage(window)
#     window.set_install_busy = lambda busy, **kwargs: page.on_busy_changed(busy)
#     manager = page._manager()
#     saved = {"name": "Saved", "host": False, "steam": True, "port": 5445, "players": 4, "address": ""}
#     monkeypatch.setattr(manager, "options", lambda: saved.copy())
#     monkeypatch.setattr(page, "_manager", lambda: manager)
#     page.refresh()
#     yield page, saved, manager
#     page.close()
#     page.deleteLater()
#     app.processEvents()


# def test_session_edits_survive_launch_error_and_refresh(session_page, monkeypatch):
#     page, _, _ = session_page
#     page.name.setText("New name")
#     page.role.setCurrentIndex(1)
#     page.port.setValue(5555)
#     page.players.setValue(2)
#     page.address.setText("192.168.1.10")
#     draft = page._options()
#     monkeypatch.setattr("commander_gui.windows.executable_pids", lambda _: set())
#     warning = Mock()
#     monkeypatch.setattr("commander_gui.ui.coop_page.QMessageBox.warning", warning)
#     page._launch()  # Missing Steam prompts before settings have been saved.
#     warning.assert_called_once()
#     assert "Start Steam" in warning.call_args.args[2]
#     assert page._options() == draft
#     page.transport.setCurrentIndex(1)
#     draft = page._options()
#     page.name.setSelection(0, 3)
#     page.refresh()
#     assert page._options() == draft
#     assert page.name.selectedText() == "New"
#     page._end("Cancelled")
#     assert page._options() == draft


# def test_session_refresh_merges_external_changes_without_overwriting_edits(session_page):
#     page, saved, _ = session_page
#     page.name.setText("Unsaved name")
#     saved.update(name="Changed in game", port=6000)
#     page.refresh()
#     assert page.name.text() == "Unsaved name"
#     assert page.port.value() == 6000
#     # A successful save accepts normalized values, then future in-game edits.
#     page.name.setText("  New name  ")
#     saved["name"] = "New name"
#     page._session_saved("Co-op settings saved.")
#     assert page.name.text() == "New name"
#     assert page.status.text() == "Co-op settings saved."
#     saved["name"] = "Changed again in game"
#     page.refresh()
#     assert page.name.text() == saved["name"]


# def test_session_drafts_follow_installation_not_selected_mo2_profile(session_page, tmp_path, monkeypatch):
#     from commander_gui.coop import CoopManager

#     page, _, first = session_page
#     page.name.setText("First draft")
#     page.window.settings.active_profile.mo2_profile = PROFILE
#     page.refresh()
#     assert page.name.text() == "First draft"
#     second = CoopManager(CliProfile(gamma=str(tmp_path / "other-gamma"), anomaly=str(tmp_path / "other-anomaly")))
#     monkeypatch.setattr(page, "_manager", lambda: second)
#     page.refresh()
#     assert page.name.text() == ""
#     page.name.setText("Second draft")
#     monkeypatch.setattr(page, "_manager", lambda: first)
#     page.refresh()
#     assert page.name.text() == "First draft"
#     monkeypatch.setattr(page, "_manager", lambda: second)
#     page.refresh()
#     assert page.name.text() == "Second draft"


def test_unvalidated_controls_are_absent_after_refresh_and_busy_cycle(app, tmp_path, monkeypatch):
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False)
    page = CoopPage(window)
    try:
        from PySide6.QtWidgets import QLabel, QPushButton

        for busy in (True, False):
            page.on_busy_changed(busy)
            page.refresh()
            for name in ("session_controls", "session_toggle", "name", "role", "transport",
                         "port", "players", "address", "launch_button", "adopt", "friend_controls"):
                assert not hasattr(page, name)
            texts = [widget.text() for cls in (QLabel, QPushButton) for widget in page.findChildren(cls)]
            assert not any("unavailable" in text.lower() or "temporarily disabled" in text.lower() for text in texts)
            assert "Play with the same mods" not in texts
            assert "Already have co-op installed?" not in texts
        assert page.download.isEnabled()
        work = Mock()
        monkeypatch.setattr(page, "_work", work)
        manager = Mock()
        monkeypatch.setattr(page, "_manager", lambda: manager)
        page.task = SimpleNamespace(cancel_event=None)
        page._install(tmp_path / "Slim.zip")
        work.call_args.args[0](lambda _: None)
        assert manager.install_zip.call_args.kwargs["adopt"] is False
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_active_mo2_profile_is_visible_across_pages_and_settings(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from commander_gui.settings import CliSettings
    from commander_gui.ui import main_window
    from commander_gui.ui.common import BackgroundTask

    profile = CliProfile(profile_name="My installation", mo2_profile="G.A.M.M.A", active=True,
                         gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))
    settings = CliSettings(profiles=[profile])
    monkeypatch.setattr(main_window, "load_settings", lambda: settings)
    monkeypatch.setattr(BackgroundTask, "start", lambda _: None)
    def create_page(self, key):
        page = QWidget()
        page.refresh = lambda: None
        return page
    monkeypatch.setattr(main_window.MainWindow, "_create_page", create_page)
    for method in ("_maybe_check_for_updates_in_background", "_check_commander_update_status", "_maybe_show_welcome"):
        monkeypatch.setattr(main_window.MainWindow, method, lambda _: None)
    window = main_window.MainWindow()
    try:
        window.show()
        for key, _ in main_window.NAV_ITEMS:
            window.set_page(key)
            assert window.active_profile_label.isVisible()
            assert "MO2: G.A.M.M.A" in window.active_profile_label.text()
            assert window.statusBar().isAncestorOf(window.active_profile_label)
            assert window.statusBar().isAncestorOf(window.change_profile_button)
            assert "1.3.1-win" in window._status_info_label.text()
        profile.mo2_profile = PROFILE
        window.refresh_settings()
        assert PROFILE in window.active_profile_label.text()
        assert "Commander profile: My installation" in window.active_profile_label.toolTip()
        window.open_settings()
        assert window.active_profile_label.isVisible()
        window.change_profile_button.click()
        assert not window._settings_open
        assert window.stack.currentWidget() is window._pages["profiles"]
        settings.profiles.clear()
        window.refresh_settings()
        assert "No active profile" in window.active_profile_label.text()
    finally:
        window.hide()
        window.deleteLater()
        app.processEvents()


def test_coop_play_requires_active_engine_and_uses_existing_launch_flow(app, tmp_path, monkeypatch):
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False,
        set_page=Mock(), _ensure_page=Mock())
    page = CoopPage(window)
    manager = page._manager()
    state = {"installed": True, "version": "1.4", "active": False}
    monkeypatch.setattr(manager, "state", lambda: state)
    monkeypatch.setattr(manager, "assert_launch", Mock())
    monkeypatch.setattr(manager, "validate_coop_profile", Mock(return_value=PROFILE))
    monkeypatch.setattr(page, "_manager", lambda: manager)
    select = Mock()
    monkeypatch.setattr(page, "_select", select)
    try:
        page.refresh()
        assert not page.play_button.isEnabled()
        page._play_coop()
        window._ensure_page.assert_not_called()
        state["active"] = True
        page.refresh()
        assert page.play_button.isEnabled()
        page.play_button.click()
        select.assert_called_once_with(PROFILE)
        manager.assert_launch.assert_called_once_with(verify_files=False)
        window.set_page.assert_called_once_with("play")
        window._ensure_page.return_value.launch_game.assert_called_once()
        state["maintenance"] = True
        page.refresh()
        assert not page.play_button.isEnabled()
        state.pop("maintenance")
        window.install_busy = True
        page.refresh()
        assert not page.play_button.isEnabled()
    finally:
        page.close()
        page.deleteLater()


def test_coop_activity_is_embedded_in_the_operating_pane(app, tmp_path):
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False)
    page = CoopPage(window)
    def busy(value, **kwargs):
        window.install_busy = value
        page.on_busy_changed(value)
    window.set_install_busy = busy
    try:
        assert not hasattr(page, "activity_card")
        assert page.progress.isHidden()
        for area, layout in (("install", page.install_layout), ("play", page.play_layout),
                             ("maintenance", page.maintenance_layout)):
            assert page._begin(area)
            assert page.progress.parentWidget() is layout.parentWidget()
            assert not page.progress.isHidden()
            page._end()
            assert page.progress.isHidden()
        assert page.maintenance_toggle.isChecked()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_normal_play_warns_when_coop_engine_is_active(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QLabel

    from commander_gui.ui.play_page import PlayPage

    profile = CliProfile(gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))
    state = {"installed": True, "active": True}
    monkeypatch.setattr("commander_gui.coop.CoopManager.state", lambda _: state)
    page = SimpleNamespace(window=SimpleNamespace(settings=SimpleNamespace(active_profile=profile)),
                           coop_warning=QLabel())
    PlayPage._refresh_coop_warning(page)
    assert "Co-op binaries are active" in page.coop_warning.text()
    assert not page.coop_warning.isHidden()
    state["active"] = False
    PlayPage._refresh_coop_warning(page)
    assert page.coop_warning.isHidden()
    assert not page.coop_warning.text()
    page.coop_warning.deleteLater()


def test_install_prompt_chooses_custom_profile_and_cancel_does_no_work(app, tmp_path, monkeypatch):
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False)
    source = tmp_path / "gamma/profiles/G.A.M.M.A"
    source.mkdir(parents=True)
    (source / "modlist.txt").write_text("+Base")
    page = CoopPage(window)
    ask = Mock(return_value=("Friends", True))
    monkeypatch.setattr("commander_gui.ui.coop_page.QInputDialog.getText", ask)
    try:
        assert page._prepare_coop_profile()
        assert page._pending_coop_profile == "Friends"
        assert ask.call_args.args[-1] == PROFILE
        assert "load order" in ask.call_args.args[2]
        assert not window.settings.active_profile.mo2_coop_profile
        assert not (source.parent / "Friends").exists()
        ask.return_value = ("", False)
        begin = Mock()
        monkeypatch.setattr(page, "_begin", begin)
        page._download()
        begin.assert_not_called()
    finally:
        page.close()
        page.deleteLater()


def test_installed_controls_and_remove_confirmation(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False)
    page = CoopPage(window)
    manager = page._manager()
    state = {"installed": True, "version": "1.4", "active": True}
    monkeypatch.setattr(manager, "state", lambda: state)
    monkeypatch.setattr(manager, "validate_coop_profile", Mock(return_value=PROFILE))
    monkeypatch.setattr(page, "_manager", lambda: manager)
    work = Mock()
    monkeypatch.setattr(page, "_work", work)
    question = Mock(return_value=QMessageBox.StandardButton.No)
    monkeypatch.setattr("commander_gui.ui.coop_page.QMessageBox.question", question)
    try:
        page.refresh()
        assert page.download.isHidden() and page.local_zip_button.isHidden()
        assert not page.remove_button.isHidden()
        page.remove_button.click()
        question.assert_called_once()
        work.assert_not_called()
        question.return_value = QMessageBox.StandardButton.Yes
        page.remove_button.click()
        work.assert_called_once()
        state["installed"] = False
        page.refresh()
        assert not page.download.isHidden() and not page.local_zip_button.isHidden()
        assert page.remove_button.isHidden()
    finally:
        page.close()
        page.deleteLater()


def test_coop_startup_check_once_and_shared_result_across_pages(app, tmp_path, monkeypatch):
    from commander_gui.coop_updates import CoopRelease
    from commander_gui.ui.coop_update_check import CoopUpdateCheck

    tasks = []
    class Task(QObject):
        result = Signal(object)
        error = Signal(str)
        def __init__(self, fn, parent=None):
            super().__init__(parent)
            self.start = Mock()
            self.cancel = Mock()
            tasks.append(self)
    monkeypatch.setattr("commander_gui.ui.coop_update_check.BackgroundTask", Task)
    service = CoopUpdateCheck()
    service.start_once()
    service.start_once()
    assert len(tasks) == 1
    release = CoopRelease("1.4", "xrRazom v1.4", "Summary", "", "https://www.moddb.com/")
    tasks[0].result.emit([release])
    service.start_once()
    assert len(tasks) == 1
    window = SimpleNamespace(settings=SimpleNamespace(active_profile=CliProfile(
        gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))), install_busy=False,
        coop_updates=service)
    pages = [CoopPage(window), CoopPage(window)]
    try:
        for page in pages:
            page.refresh()
            assert page.latest_value.text() == "1.4"
            assert page.notes_layout.count() == 1
        assert len(tasks) == 1
        pages[0].check_updates_button.click()
        assert len(tasks) == 2
        assert all(not page.check_updates_button.isEnabled() for page in pages)
        tasks[1].error.emit("Offline")
        service.start_once()
        assert len(tasks) == 2
        assert all("Offline" in page.release_status.text() for page in pages)
        service.check()
        service.before_ui_rebuild()
        assert service.task is None
        assert "interrupted" in service.error
        tasks[2].result.emit([release])
        assert "interrupted" in service.error
        service.start_once()
        assert len(tasks) == 3
    finally:
        for page in pages:
            page.close()
            page.deleteLater()
        service.deleteLater()


def test_profiles_editor_keeps_separate_normal_and_coop_choices(app, tmp_path):
    from commander_gui.settings import CliSettings
    from commander_gui.ui.profiles_page import ProfilesPage

    profile = CliProfile(active=True, profile_name="GAMMA", gamma=str(tmp_path / "gamma"),
                         anomaly=str(tmp_path / "anomaly"), mo2_profile="Friends",
                         mo2_singleplayer_profile="G.A.M.M.A", mo2_coop_profile="Friends")
    window = SimpleNamespace(settings=CliSettings(profiles=[profile]), install_busy=False,
                             refresh_settings=lambda: None)
    page = ProfilesPage(window)
    try:
        page._form_state = "GAMMA"
        page._load_form(profile)
        assert page.mo2_edit.text() == "G.A.M.M.A"
        assert page.coop_edit.text() == "Friends"
        edited = page._form_values()
        assert edited.singleplayer_profile == "G.A.M.M.A"
        assert edited.mo2_coop_profile == "Friends"
        assert edited.mo2_profile == "Friends"
        page._new_profile()
        assert not page.coop_edit.text()
    finally:
        page.close()
        page.deleteLater()
