"""Windows notification-area messages, delivered on Qt's GUI thread."""

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon

from .config import project_root


class DesktopNotifications(QObject):
    requested = Signal(str, str)

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.tray = QSystemTrayIcon(self)
        icon = app.windowIcon()
        if icon.isNull():
            icon = QIcon(str(project_root() / "cli/stalker-gamma.png"))
        self.tray.setIcon(icon)
        self.tray.setToolTip("STALKER GAMMA Commander")
        self.tray.messageClicked.connect(self._show_window)
        self.tray.activated.connect(self._activated)
        self.requested.connect(self._show, Qt.ConnectionType.QueuedConnection)
        app.aboutToQuit.connect(self.tray.hide)

    @Slot(str, str)
    def _show(self, title, message):
        if QSystemTrayIcon.isSystemTrayAvailable() and QSystemTrayIcon.supportsMessages():
            self.tray.show()
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 10000)

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    def _show_window(self):
        for window in self.app.topLevelWidgets():
            if isinstance(window, QMainWindow):
                if window.isMinimized():
                    window.showNormal()
                else:
                    window.show()
                window.raise_()
                window.activateWindow()
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
