"""Per-addon download activity shared by installs and updates."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
)

from ..i18n import tr
from ..integrity import format_size
from ..parsers import ProgressEvent


class ProgressTable(QTableWidget):
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels([tr("Addon"), tr("Status"), tr("Progress"), tr("Speed")])
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(38)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSortingEnabled(False)
        self.setWordWrap(False)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Stretch)
        self.setColumnWidth(1, 155)
        self.setColumnWidth(2, 185)
        self.setColumnWidth(3, 100)
        self._rows: dict[str, int] = {}
        self._events: dict[str, ProgressEvent] = {}
        self._finished: set[str] = set()
        self._interrupted: set[str] = set()
        self._updated: dict[str, float] = {}
        self._show_completed = False
        self._concurrency = 6
        self._paused = False
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._expire_speed)

    def reset(self) -> None:
        self._paused = False
        self._timer.stop()
        self.setRowCount(0)
        self._rows.clear()
        self._events.clear()
        self._finished.clear()
        self._interrupted.clear()
        self._updated.clear()
        self.changed.emit()

    def set_concurrency(self, threads: int) -> None:
        self._concurrency = max(1, threads)

    def set_show_completed(self, enabled: bool) -> None:
        self._show_completed = enabled
        for name, row in self._rows.items():
            self.setRowHidden(row, name in self._finished and not enabled)

    @property
    def summary(self) -> str:
        return tr("{count} addons · {finished} finished", count=len(self._rows), finished=len(self._finished))

    def upsert(self, event: ProgressEvent) -> None:
        old = self._events.get(event.name)
        if old is not None and old.structured and not event.structured:
            return
        self._events[event.name] = event
        self._updated[event.name] = time.monotonic()
        self._interrupted.discard(event.name)
        row = self._rows.get(event.name)
        if row is None:
            row = self.rowCount()
            self.insertRow(row)
            self._rows[event.name] = row
            name_item = QTableWidgetItem(event.name)
            name_item.setToolTip(event.name)
            self.setItem(row, 0, name_item)
            self.setItem(row, 1, QTableWidgetItem())
            self.setItem(row, 2, QTableWidgetItem())
            self.setItem(row, 3, QTableWidgetItem())
            self.item(row, 3).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            bar = QProgressBar(self)
            bar.setObjectName("addonProgress")
            bar.setAccessibleName(tr("{name} progress", name=event.name))
            self.setCellWidget(row, 2, bar)
        percent = max(0.0, min(1.0, event.percent))
        done = event.operation in {"Complete", "Skipped"} or (
            not event.structured and event.operation in {"Extract", "Expand"} and percent >= 1
        )
        if done:
            self._finished.add(event.name)
        else:
            self._finished.discard(event.name)
        # Explicitly re-show a row when extraction, retry, or a new phase starts.
        self.setRowHidden(row, done and not self._show_completed)
        status = {
            "Download": tr("Downloaded") if percent >= 1 else tr("Downloading"),
            "Extract": tr("Finalizing") if event.structured and percent >= 1 else tr("Extracting"),
            "Expand": tr("Expanding"), "Check MD5": tr("Checking archive"),
            "Skipped": tr("Up to date"), "Complete": tr("Complete"),
            "Queued": tr("Queued"), "Resolving": tr("Getting download link"),
            "Retrying": tr("Retrying"), "Resuming": tr("Resuming"),
            "Verifying": tr("Verifying archive"), "Downloaded": tr("Downloaded"),
            "Failed": tr("Failed"),
        }.get(event.operation, event.operation)
        if done and event.operation in {"Extract", "Expand"}:
            status = tr("Extracted")
        self.item(row, 1).setText(status)
        bar = self.cellWidget(row, 2)
        busy = event.operation in {"Queued", "Resolving", "Retrying", "Verifying"} or (
            event.structured and event.operation in {"Download", "Resuming"} and event.total_bytes is None
        )
        bar.setRange(0, 0 if busy else 1000)
        value = 1.0 if done else percent
        if not busy:
            bar.setValue(round(value * 1000))
        bar.setFormat(status if busy else f"{value:.1%}")
        self._style_bar(bar, "complete" if done else "active")
        detail = status
        if event.bytes_downloaded is not None:
            detail += " · " + format_size(event.bytes_downloaded)
            if event.total_bytes is not None:
                detail += " / " + format_size(event.total_bytes)
        bar.setToolTip(detail)
        speed = event.bytes_per_second
        self.item(row, 3).setText(
            format_size(speed) + "/s" if event.operation in {"Download", "Resuming"} and speed is not None and speed > 0 else "—"
        )
        if self._paused and not done:
            self._paint_paused(row, event)
        else:
            self._timer.start()
        self.changed.emit()

    def _paint_paused(self, row, event):
        self.item(row, 1).setText(tr("Paused"))
        self.item(row, 3).setText("—")
        bar = self.cellWidget(row, 2)
        bar.setRange(0, 1000)
        bar.setValue(round(event.percent * 1000))
        bar.setFormat(tr("Paused"))

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._timer.stop()
        for name, event in list(self._events.items()):
            if name in self._finished:
                continue
            if paused:
                self._paint_paused(self._rows[name], event)
            else:
                self.upsert(event)
                self.item(self._rows[name], 3).setText("—")


    @staticmethod
    def _style_bar(bar: QProgressBar, state: str) -> None:
        if bar.property("activityState") != state:
            bar.setProperty("activityState", state)
            bar.style().unpolish(bar)
            bar.style().polish(bar)

    def _expire_speed(self) -> None:
        if self._paused:
            return
        now = time.monotonic()
        for name, event in self._events.items():
            if name in self._finished or name in self._interrupted:
                continue
            if event.operation in {"Download", "Resuming"} and now - self._updated[name] > 3:
                row = self._rows[name]
                self.item(row, 3).setText("—")
                if event.percent < 1 and now - self._updated[name] > 8:
                    self.item(row, 1).setText(tr("Waiting for data"))

    def finish_all(self) -> None:
        self._timer.stop()
        for name, row in self._rows.items():
            if name not in self._finished:
                self.item(row, 1).setText(tr("Complete"))
                bar = self.cellWidget(row, 2)
                bar.setRange(0, 1000)
                bar.setValue(1000)
                bar.setFormat("100.0%")
                self._style_bar(bar, "complete")
            self.item(row, 3).setText("—")
            self._finished.add(name)
            self.setRowHidden(row, not self._show_completed)
        self.changed.emit()

    def mark_interrupted(self) -> None:
        self._timer.stop()
        for name, row in self._rows.items():
            if name in self._finished:
                continue
            self._interrupted.add(name)
            self.item(row, 1).setText(tr("Interrupted"))
            self.item(row, 3).setText("—")
            bar = self.cellWidget(row, 2)
            bar.setRange(0, 1000)
            bar.setValue(round(self._events[name].percent * 1000))
            bar.setFormat(f"{self._events[name].percent:.1%}")
            self._style_bar(bar, "interrupted")
            self.setRowHidden(row, False)
        self.changed.emit()
