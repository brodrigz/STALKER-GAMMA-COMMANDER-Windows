"""Windows prerequisite setup shared by Install and System Check."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..windows_runtimes import check_runtimes, install_missing_runtimes, runtime_summary
from .common import StreamTask, game_running, notify_desktop


class RuntimeSetupDialog(QDialog):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self._task = None
        self._running = False
        self.setWindowTitle("Windows game runtimes")
        self.resize(800, 510)
        layout = QVBoxLayout(self)
        explanation = QLabel("Checks Visual C++ and legacy DirectX libraries used by Anomaly and Mod Organizer. "
                             "Install missing downloads signed installers from Microsoft. Windows may ask for administrator permission.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Component", "Status", "Details"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 100)
        layout.addWidget(self.table)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        self.log.setMaximumHeight(130)
        layout.addWidget(self.log)
        buttons = QHBoxLayout()
        self.install_button = QPushButton("Install missing")
        self.install_button.clicked.connect(self._install)
        self.refresh_button = QPushButton("Recheck")
        self.refresh_button.clicked.connect(lambda: self.refresh())
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.install_button)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self, checks=None):
        if self._running:
            return
        checks = checks if checks is not None else check_runtimes()
        self.table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            state = "Installed" if check.installed else ("Unknown" if check.installed is None else "Missing / old")
            for column, value in enumerate((check.name, state, check.detail)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        self.table.resizeRowsToContents()
        ready, summary = runtime_summary(checks)
        self.status.setText(summary)
        self.install_button.setEnabled(ready is not True and not self.window.install_busy)

    def _install(self):
        if self._running or self.window.install_busy:
            return
        if game_running(force=True):
            self.status.setText("Close Mod Organizer and Anomaly before installing runtimes.")
            return
        self._running = True
        self.window.set_install_busy(True, "dependencies")
        self.install_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.close_button.setText("Stop after current step")
        self.progress.show()
        self.log.clear()
        task = StreamTask(lambda report: install_missing_runtimes(report, task.cancel_event), parent=self)
        self._task = task
        task.line.connect(self._on_line)
        task.result.connect(self._done)
        task.error.connect(self._error)
        task.start()

    def _on_line(self, message):
        self.status.setText(message)
        self.log.appendPlainText(message)

    def _finish(self):
        self._running = False
        self.window.set_install_busy(False)
        self.refresh_button.setEnabled(True)
        self.close_button.setEnabled(True)
        self.close_button.setText("Close")
        self.progress.hide()
        # The result signal can arrive just before QThread has fully stopped.
        # Retain the task parent until its normal finished/shutdown cleanup.

    def _done(self, result):
        self._finish()
        self.refresh(result["checks"])
        ready, _ = runtime_summary(result["checks"])
        message = result["message"] or ("All required runtimes are installed." if ready else "Setup finished, but some runtime checks still fail. Review the details above.")
        if result["reboot"]:
            message += " Restart Windows before playing; Commander will not restart it automatically."
        self._on_line(message)
        notify_desktop("Windows runtime setup", message)

    def _error(self, message):
        self._finish()
        self.refresh()
        self._on_line("Setup failed: " + message)

    def reject(self):
        if self._running:
            self._task.cancel()
            self.close_button.setEnabled(False)
            self.status.setText("Stopping after the current step. Finish or cancel the open Microsoft installer to continue.")
            return
        if self._task is not None:
            self._task.shutdown()
        super().reject()

    def closeEvent(self, event):
        if self._running:
            self.reject()
            event.ignore()
        else:
            if self._task is not None:
                self._task.shutdown()
            super().closeEvent(event)


def show_runtime_setup(window, parent=None):
    dialog = RuntimeSetupDialog(window, parent)
    dialog.exec()
    dialog.deleteLater()
