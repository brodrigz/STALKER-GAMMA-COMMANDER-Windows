"""Access panels appear on demand, with or without a download table."""

import json
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from commander_gui.moddb_session import ACCESS_PREFIX
from commander_gui.ui.common import ProgressArea


@pytest.mark.parametrize("show_table", [True, False])
def test_access_visibility_follows_session(show_table):
    app = QApplication.instance() or QApplication([])
    area = ProgressArea(show_table=show_table, auto_expand_log=False)
    runner = Mock()
    runner.verify_moddb.return_value = True
    area.set_runner(runner)
    panel = area.moddb_access

    def event(state):
        area.on_line(ACCESS_PREFIX + json.dumps({"state": state, "message": state}))

    try:
        area.show()
        app.processEvents()
        assert panel.isHidden()
        area.on_started()
        assert panel.isHidden()
        event("not_needed")
        assert panel.isHidden()
        event("required")
        assert panel.isVisible()
        assert panel.button.isEnabled()
        runner.verify_moddb.assert_not_called()
        panel.button.click()
        runner.verify_moddb.assert_called_once()
        assert panel.isVisible() and panel._state == "verifying"
        event("failed")
        assert panel.isVisible() and panel.button.isEnabled()
        event("ready")
        assert panel.isVisible() and not panel.button.isEnabled()
        area.on_finished(0, "Completed")
        assert panel.isHidden()
        event("required")  # Late events cannot reopen an ended session.
        assert panel.isHidden()
        area.reset()
        area.on_started()
        event("required")
        event("not_needed")
        assert panel.isHidden()
        event("rate_limited")
        area.on_finished(1, "Too many requests")
        assert panel.isVisible() and not panel.button.isEnabled()
        area.reset()
        assert panel.isHidden()
    finally:
        area.close()
        area.deleteLater()
        app.processEvents()
