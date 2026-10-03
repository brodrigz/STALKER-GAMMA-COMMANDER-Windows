import json
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from commander_gui.parsers import PROGRESS_PREFIX, parse_progress_line
from commander_gui.ui.common import ProgressArea


def event(name="Addon", operation="Download", percent=0.5, **kwargs):
    return PROGRESS_PREFIX + json.dumps({"version": 1, "name": name, "operation": operation,
                                        "percent": percent, "complete": 0, "total": 20, **kwargs})


@pytest.fixture
def area():
    app = QApplication.instance() or QApplication([])
    widget = ProgressArea(resizable_log=True)
    yield widget
    widget.close()
    widget.deleteLater()
    app.sendPostedEvents()


def test_independent_bars_speed_and_bytes(area):
    area.on_line(event("First", percent=0.25, bytesDownloaded=1024, totalBytes=4096, bytesPerSecond=2048))
    area.on_line(event("Second", percent=0.75, bytesDownloaded=3072, totalBytes=4096, bytesPerSecond=8192))
    assert area.table.cellWidget(0, 2).value() == 250
    assert area.table.cellWidget(1, 2).value() == 750
    assert area.table.item(0, 3).text() == "2.0 KB/s"
    assert area.table.item(1, 3).text() == "8.0 KB/s"
    assert "1.0 KB / 4.0 KB" in area.table.cellWidget(0, 2).toolTip()
    area.on_line("First | Download | 10% | [0/20]")
    assert area.table.cellWidget(0, 2).value() == 250


def test_phase_transitions_completed_filter_and_retry(area):
    area.on_line(event(operation="Download", percent=1, totalBytes=100, bytesDownloaded=100))
    assert not area.table.isRowHidden(0)
    area.on_line(event(operation="Extract", percent=0.2))
    assert area.table.cellWidget(0, 2).value() == 200
    area.on_line(event(operation="Extract", percent=1))
    assert not area.table.isRowHidden(0)  # Wait for actual CLI completion.
    area.on_line(event(operation="Complete", percent=1))
    assert area.table.isRowHidden(0)
    area.completed_toggle.setChecked(True)
    assert not area.table.isRowHidden(0)
    assert area.table.item(0, 1).text() == "Complete"
    area.completed_toggle.setChecked(False)
    area.on_line(event(operation="Retrying", percent=0.5))
    assert not area.table.isRowHidden(0)
    assert area.table.cellWidget(0, 2).maximum() == 0


def test_legacy_extraction_row_reappears(area):
    area.on_line("Addon | Extract | 100% | [1/20]")
    assert area.table.isRowHidden(0)
    area.on_line("Addon | Extract | 20% | [1/20]")
    assert not area.table.isRowHidden(0)


def test_expired_speed_and_interrupted_busy_state(area):
    with patch("commander_gui.ui.download_activity.time.monotonic", return_value=10):
        area.on_line(event(bytesDownloaded=50, totalBytes=100, bytesPerSecond=8192))
    with patch("commander_gui.ui.download_activity.time.monotonic", return_value=20):
        area.table._expire_speed()
    assert area.table.item(0, 3).text() == "—"
    assert area.table.item(0, 1).text() == "Waiting for data"
    area.on_line(event(operation="Resolving"))
    area.on_cancelled()
    assert area.table.item(0, 1).text() == "Interrupted"
    assert area.table.cellWidget(0, 2).maximum() == 1000
    assert not area.table._timer.isActive()


def test_resize_reset_and_console_independence(area):
    area.show()
    area.on_started()
    area.on_line(event(totalBytes=100, bytesDownloaded=50))
    area.table_resize_handle._resize(420)
    assert area.table.height() == 420
    area._toggle_log()
    assert area.log.isHidden()
    assert not area.table_panel.isHidden()
    assert not area.bar.isHidden()
    area.reset()
    assert area.table.rowCount() == 0
    assert not area._structured_names
    assert not area.table._timer.isActive()


@pytest.mark.parametrize("change", [
    {"version": 2}, {"percent": float("nan")}, {"bytesPerSecond": -1},
    {"bytesDownloaded": "10"}, {"total": True}, {"name": []}, {"operation": "unknown"},
])
def test_rejects_invalid_protocol(change):
    assert parse_progress_line(event(**change)) is None


def test_portuguese_percentage_output_still_works():
    assert parse_progress_line("Addon | Download | 12,34% | [2/20]").percent == 0.1234


def test_browser_notice_survives_other_addon_progress_with_console_hidden(area):
    area._auto_expand_log = False
    area.show()
    area.on_started()
    area.on_line("@commander-status Complete verification in the browser.")
    area.on_line(event("Other addon"))
    assert area.log.isHidden()
    assert not area.table_panel.isHidden()
    assert not area.connection_notice.isHidden()
    assert area.connection_notice.text() == "Complete verification in the browser."
    area.reset()
    assert area.connection_notice.isHidden()
