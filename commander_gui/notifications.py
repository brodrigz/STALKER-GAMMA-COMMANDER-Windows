"""Persistent Windows tray controls and GUI-thread notification delivery."""

import weakref

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QStyle, QSystemTrayIcon
from shiboken6 import isValid

from .config import project_root


class DesktopNotifications(QObject):
    requested = Signal(str, str)

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.window = None
        self._exiting = False
        self._attention = {}
        self._attention_states = {}
        self._observed_sources = weakref.WeakSet()
        self.tray = QSystemTrayIcon(self)
        icon = app.windowIcon()
        if icon.isNull():
            icon = QIcon(str(project_root() / "cli/stalker-gamma.png"))
        self.tray.setIcon(icon)
        self._icon = icon
        self.tray.setToolTip("STALKER GAMMA Commander")
        self.tray.messageClicked.connect(self._show_window)
        self.tray.activated.connect(self._activated)
        self.requested.connect(self._show, Qt.ConnectionType.QueuedConnection)
        app.aboutToQuit.connect(self.tray.hide)
        app.commitDataRequest.connect(self._session_shutdown)

    def attach_window(self, window):
        self.window = window
        window.installEventFilter(self)
        self.menu = QMenu(window)
        self.menu.addAction("Show Commander", self._show_window)
        self.menu.addAction("Hide to tray", self.hide_window)
        self.verification_action = self.menu.addAction("Cloudflare verification required…", self._show_window)
        self.verification_action.setVisible(False)
        self.menu.addSeparator()
        self.menu.addAction("Exit", self.request_exit)
        self.tray.setContextMenu(self.menu)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.app.setQuitOnLastWindowClosed(False)
            self.tray.show()

    def _session_shutdown(self, _manager):
        self._exiting = True

    def eventFilter(self, watched, event):
        if watched is self.window and not self._exiting:
            available = QSystemTrayIcon.isSystemTrayAvailable()
            if event.type() == QEvent.Type.Close:
                if available and not self.app.isSavingSession():
                    self.hide_window()
                    event.ignore()
                    return True
                # If Explorer/tray is unavailable, keep ordinary window exit usable.
                self.app.setQuitOnLastWindowClosed(True)
            elif available and event.type() == QEvent.Type.WindowStateChange and watched.isMinimized():
                QTimer.singleShot(0, self._hide_minimized)
        return super().eventFilter(watched, event)

    def _hide_minimized(self):
        if self.window is not None and self.window.isMinimized() and not self._exiting:
            self.hide_window()

    def hide_window(self):
        if self.window is None or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.app.setQuitOnLastWindowClosed(False)
        self.tray.show()
        self.window.hide()

    def request_exit(self):
        if self.window is not None:
            # Native prerequisite installers cannot safely be interrupted.
            if self.app.activeModalWidget() is not None:
                self._show_window()
                return
            self._show_window()
            self._exiting = True
            try:
                if not self.window.close():
                    return
            finally:
                self._exiting = False
        self.tray.hide()
        self.app.quit()

    def set_verification_required(self, source, required):
        key = id(source)
        state = getattr(source, "_state", None)
        is_new = required and (key not in self._attention or self._attention_states.get(key) != state)
        if is_new:
            self._attention[key] = weakref.ref(source)
            self._attention_states[key] = state
            if source not in self._observed_sources:
                self._observed_sources.add(source)
                source.destroyed.connect(self._remove_destroyed_sources)
        elif not required:
            self._attention.pop(key, None)
            self._attention_states.pop(key, None)
        self._refresh_attention()
        if is_new:
            if QSystemTrayIcon.isSystemTrayAvailable() and QSystemTrayIcon.supportsMessages():
                self.tray.show()
                self.tray.showMessage(
                    "ModDB is limiting requests" if state == "rate_limited" else "Downloads need Cloudflare verification",
                    source.instructions.text() if state == "rate_limited" else "Open Commander and click Verify in browser. Saved download progress is kept.",
                    QSystemTrayIcon.MessageIcon.Warning, 30000,
                )
            if self.window is not None and not self.window.isActiveWindow():
                QApplication.alert(self.window)

    @Slot()
    def _remove_destroyed_sources(self):
        self._attention = {key: ref for key, ref in self._attention.items()
                           if ref() is not None and isValid(ref())}
        self._attention_states = {key: state for key, state in self._attention_states.items() if key in self._attention}
        self._refresh_attention()

    def _refresh_attention(self):
        required = bool(self._attention)
        limited = any(state == "rate_limited" for state in self._attention_states.values())
        self.tray.setIcon(self.app.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning) if required else self._icon)
        self.tray.setToolTip("Commander — ModDB rate limit" if limited else "Commander — Cloudflare verification required" if required else "STALKER GAMMA Commander")
        if self.window is not None and isValid(self.window):
            self.verification_action.setVisible(required)
            self.verification_action.setText("ModDB rate limit — details…" if limited else "Cloudflare verification required…")
            if limited:
                self.window.set_verification_required(True, "ModDB is limiting requests. Wait before restarting downloads; saved progress is kept.")
            else:
                self.window.set_verification_required(required)

    @Slot(str, str)
    def _show(self, title, message):
        if QSystemTrayIcon.isSystemTrayAvailable() and QSystemTrayIcon.supportsMessages():
            self.tray.show()
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 10000)

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    def _show_window(self):
        windows = [self.window] if self.window is not None else self.app.topLevelWidgets()
        for window in windows:
            if isinstance(window, QMainWindow):
                if window.isMinimized():
                    window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
                window.show()
                window.raise_()
                window.activateWindow()
                for source_ref in self._attention.values():
                    source = source_ref()
                    if source is not None:
                        window.reveal_verification(source)
                        break
                modal = self.app.activeModalWidget()
                if modal is not None:
                    modal.raise_()
                    modal.activateWindow()
                return


def initialize_notifications(app=None):
    app = app or QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        return None
    if not hasattr(app, "_desktop_notifications"):
        app._desktop_notifications = DesktopNotifications(app)
    return app._desktop_notifications


def notify(title: str, message: str):
    app = QApplication.instance()
    service = getattr(app, "_desktop_notifications", None) or initialize_notifications(app)
    if service is not None:
        service.requested.emit(title, message)
