"""One shared xrRazom release check per app startup, with manual refresh."""
from datetime import datetime, timezone

from PySide6.QtCore import QObject, Signal

from ..coop_updates import check_coop_releases
from .common import BackgroundTask


class CoopUpdateCheck(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started = False
        self.task = None
        self.releases = []
        self.error = ""
        self.checked_at = None

    def start_once(self):
        if not self.started:
            self.check()

    def check(self):
        if self.task is not None:
            return
        self.started = True
        self.error = ""
        self.task = BackgroundTask(check_coop_releases, parent=self)
        self.task.result.connect(self._loaded)
        self.task.error.connect(self._failed)
        self.changed.emit()
        self.task.start()

    def _loaded(self, releases):
        self.task = None
        self.releases = releases
        self.checked_at = datetime.now(timezone.utc).astimezone()
        self.changed.emit()

    def _failed(self, message):
        self.task = None
        self.error = message
        self.changed.emit()

    def before_ui_rebuild(self):
        # MainWindow shuts down background tasks when rebuilding translated UI.
        # Do not leave a cancelled check looking busy or automatically retry it.
        if self.task is not None:
            self.task.result.disconnect(self._loaded)
            self.task.error.disconnect(self._failed)
            self.task.cancel()
            self._failed("Check interrupted by interface reload. Click Check for updates to retry.")
