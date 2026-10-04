"""Optional local snapshot check, separate from publisher verification."""
import threading

from PySide6.QtWidgets import QMessageBox, QPushButton, QVBoxLayout, QWidget

from ..integrity import scan_mods_md5
from .common import ProgressArea, StreamTask, info_label, mo2_running, section_label, tr


class LocalMd5Pane(QWidget):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.task = None
        self.cancel = None
        layout = QVBoxLayout(self)
        layout.addWidget(section_label(tr("Local MD5 Check"), level=2))
        layout.addWidget(info_label(tr(
            "Compare installed mod files with your saved local snapshot. The first run creates "
            "the snapshot; it cannot detect corruption already present. This does not verify publisher hashes."
        )))
        self.button = QPushButton(tr("Create / Check Local MD5"))
        self.button.clicked.connect(self.start)
        layout.addWidget(self.button)
        self.progress = ProgressArea(show_table=False, show_log=True, log_max_height=180)
        self.progress.cancel_button.clicked.connect(self.stop)
        layout.addWidget(self.progress)

    def refresh(self):
        self.button.setEnabled(not self.window.install_busy and self.window.settings.active_profile is not None)

    def start(self):
        profile = self.window.settings.active_profile
        if self.window.install_busy or profile is None:
            return
        if mo2_running(force=True):
            QMessageBox.information(self, tr("Game Running"), tr("Close Mod Organizer and the game before checking files."))
            return
        self.cancel = threading.Event()
        gamma, cancel = profile.gamma, self.cancel
        self.window.set_install_busy(True)
        self.refresh()
        self.progress.reset()
        self.progress.on_started()
        self.progress.pause_button.hide()
        self.progress.bar.setRange(0, 0)
        task = StreamTask(lambda report: scan_mods_md5(
            gamma, cancel=cancel,
            on_progress=lambda done, total, size: report(f"Local MD5: {done}/{total} files ({size})")), parent=self)
        task.line.connect(self.progress.status_message)
        task.result.connect(self.finished)
        task.error.connect(self.failed)
        self.task = task
        task.start()

    def stop(self):
        if self.cancel is not None:
            self.cancel.set()

    def finished(self, result):
        for line in result.lines():
            self.progress.log.append_line(line)
        if result.cancelled:
            self.progress.on_cancelled()
        elif result.created:
            self.progress.set_success_state("Local baseline created — publisher integrity not checked")
        elif result.problems:
            self.progress.on_finished(1, "")
            self.progress.status_message(result.summary)
        else:
            self.progress.set_success_state("Unchanged since local baseline")
        self.release()

    def failed(self, message):
        self.progress.log.append_line(message)
        self.progress.on_finished(1, "")
        self.release()

    def release(self):
        self.task = None
        self.cancel = None
        self.window.set_install_busy(False)
        self.refresh()
