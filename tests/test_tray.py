"""Tray lifecycle and verification attention without network/browser access."""

import json
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
)

from commander_gui import notifications
from commander_gui.moddb_session import ACCESS_PREFIX
from commander_gui.ui.moddb_access import ModDbAccessPanel


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.close_calls = 0
        self.allow_exit = False
        self.set_verification_required = Mock()
        self.reveal_verification = Mock()

    def closeEvent(self, event):
        self.close_calls += 1
        event.setAccepted(self.allow_exit)


@pytest.fixture
def tray(monkeypatch):
    app = QApplication.instance() or QApplication([])
    original_quit = app.quitOnLastWindowClosed()
    native = notifications.QSystemTrayIcon
    factory = Mock(return_value=Mock())
    factory.isSystemTrayAvailable.return_value = True
    factory.supportsMessages.return_value = True
    factory.MessageIcon = native.MessageIcon
    factory.ActivationReason = native.ActivationReason
    monkeypatch.setattr(notifications, "QSystemTrayIcon", factory)
    service = notifications.DesktopNotifications(app)
    monkeypatch.setattr(app, "_desktop_notifications", service, raising=False)
    window = Window()
    service.attach_window(window)
    window.show()
    app.processEvents()
    yield app, window, service, factory
    window.removeEventFilter(service)
    window.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    service.window = None
    service.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.setQuitOnLastWindowClosed(original_quit)


def test_close_and_minimize_keep_operation_alive_and_restore(tray):
    app, window, service, _ = tray
    assert not app.quitOnLastWindowClosed()
    assert not window.close()
    assert window.isHidden()
    assert window.close_calls == 0
    service._show_window()
    assert window.isVisible()
    window.showMaximized()
    window.showMinimized()
    app.processEvents()
    assert window.isHidden()
    service._show_window()
    app.processEvents()
    assert window.isVisible() and not window.isMinimized()
    assert window.windowState() & Qt.WindowState.WindowMaximized
    assert window.close_calls == 0


def test_tray_exit_honors_cancel_then_shuts_down(tray, monkeypatch):
    app, window, service, _ = tray
    quit_app = Mock()
    monkeypatch.setattr(app, "quit", quit_app)
    exit_action = service.menu.actions()[-1]
    assert exit_action.text() == "Exit"
    service.hide_window()
    exit_action.trigger()
    assert window.close_calls == 1 and window.isVisible()
    quit_app.assert_not_called()
    window.allow_exit = True
    exit_action.trigger()
    assert window.close_calls == 2
    quit_app.assert_called_once()


def test_missing_tray_falls_back_to_ordinary_close(tray):
    app, window, _service, factory = tray
    factory.isSystemTrayAvailable.return_value = False
    window.allow_exit = True
    assert window.close()
    assert window.close_calls == 1
    assert app.quitOnLastWindowClosed()


def test_exit_does_not_bypass_modal_installer(tray, monkeypatch):
    app, window, service, _ = tray
    quit_app = Mock()
    monkeypatch.setattr(app, "quit", quit_app)
    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.show()
    app.processEvents()
    service.request_exit()
    quit_app.assert_not_called()
    assert window.close_calls == 0 and dialog.isVisible()
    dialog.close()


def test_verification_warns_once_routes_click_and_clears(tray):
    _app, window, service, _ = tray
    verify = Mock(return_value=True)
    panel = ModDbAccessPanel(verify, window)
    panel.start()
    event = ACCESS_PREFIX + json.dumps({"state": "required", "message": "ModDB rejected the request (403)."})
    service.hide_window()
    panel.consume_line(event)
    panel.consume_line(event)
    service.tray.showMessage.assert_called_once()
    assert service.tray.showMessage.call_args.args[2] == notifications.QSystemTrayIcon.MessageIcon.Warning
    assert service.verification_action.isVisible()
    window.set_verification_required.assert_called_with(True)
    assert "⚠" in panel.status.text()
    assert panel.button.isEnabled()
    service._show_window()
    window.reveal_verification.assert_called_with(panel)
    verify.assert_not_called()
    panel.button.click()
    verify.assert_called_once()
    assert not service.verification_action.isVisible()
    panel.consume_line(ACCESS_PREFIX + json.dumps({"state": "failed", "message": "Try again."}))
    assert service.tray.showMessage.call_count == 2
    panel.finish()
    window.set_verification_required.assert_called_with(False)
    assert not service.verification_action.isVisible()


def test_attention_survives_other_panels_and_clears_on_destruction(tray):
    _app, window, service, _ = tray
    first = ModDbAccessPanel(Mock(), window)
    second = ModDbAccessPanel(Mock(), window)
    first.start()
    second.start()
    first._set_state("required", "Verify")
    second._set_state("required", "Verify")
    first.finish()
    assert service.verification_action.isVisible()
    second.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not service.verification_action.isVisible()
    window.set_verification_required.assert_called_with(False)


def test_warning_remains_visible_when_os_suppresses_balloons(tray):
    _app, window, service, factory = tray
    factory.supportsMessages.return_value = False
    panel = ModDbAccessPanel(Mock(), window)
    panel.start()
    panel._set_state("required", "Verify")
    service.tray.showMessage.assert_not_called()
    window.set_verification_required.assert_called_with(True)
    assert service.verification_action.isVisible()


def test_global_banner_navigates_to_waiting_download_with_console_hidden(tray, monkeypatch, tmp_path):
    from commander_gui import gui_settings
    from commander_gui.settings import CliSettings
    from commander_gui.ui import main_window
    from commander_gui.ui.common import BackgroundTask

    app, original, service, _ = tray
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(main_window, "load_settings", lambda: CliSettings(profiles=[]))
    monkeypatch.setattr(gui_settings, "save_gui_settings", Mock())
    monkeypatch.setattr(BackgroundTask, "start", lambda self: None)
    for method in ("_maybe_check_for_updates_in_background", "_check_commander_update_status", "_maybe_show_welcome"):
        monkeypatch.setattr(main_window.MainWindow, method, lambda self: None)
    window = main_window.MainWindow()
    service.attach_window(window)
    try:
        updates = window._ensure_page("update")
        area = updates.apply_progress
        runner = Mock()
        runner.verify_moddb.return_value = True
        area.set_runner(runner)
        area.on_started()
        window.set_page("dashboard")
        window.show()
        area.on_line(ACCESS_PREFIX + json.dumps({"state": "required", "message": "HTTP 403. Verify to continue."}))
        assert not window.verification_banner.isHidden()
        assert area.log.isHidden()
        window.verification_banner.findChild(QPushButton).click()
        assert window.stack.currentWidget() is updates
        window.set_page("dashboard")
        window.install_busy = True
        question = Mock(return_value=QMessageBox.StandardButton.No)
        monkeypatch.setattr(main_window.QMessageBox, "question", question)
        quit_app = Mock()
        monkeypatch.setattr(app, "quit", quit_app)
        window.close()
        assert window.isHidden()
        question.assert_not_called()
        assert window.install_busy
        service.request_exit()
        question.assert_called_once()
        quit_app.assert_not_called()
        assert window.isVisible()
        service.hide_window()
        service._show_window()
        app.processEvents()
        assert window.isVisible()
        assert window.stack.currentWidget() is updates
        assert area.moddb_access.button.isEnabled()
        runner.verify_moddb.assert_not_called()
        area.moddb_access.button.click()
        assert window.verification_banner.isHidden()
        runner.verify_moddb.assert_called_once()
        area.on_cancelled()
        question.return_value = QMessageBox.StandardButton.Yes
        service.request_exit()
        quit_app.assert_called_once()
    finally:
        window.removeEventFilter(service)
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        service.attach_window(original)
