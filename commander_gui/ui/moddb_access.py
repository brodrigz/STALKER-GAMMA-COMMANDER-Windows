"""Compact, user-controlled ModDB browser verification status."""

import json
import time
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..moddb_session import ACCESS_PREFIX


class ModDbAccessPanel(QWidget):
    def __init__(self, verify, parent=None):
        super().__init__(parent)
        self.setObjectName("moddbAccess")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._verify = verify
        self._state = "idle"
        self._active = False
        self._expires = None
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.button = QPushButton("Verify in browser")
        self.button.setObjectName("secondary")
        self.button.clicked.connect(self._clicked)
        self.instructions = QLabel()
        self.instructions.setObjectName("info")
        self.instructions.setTextFormat(Qt.TextFormat.PlainText)
        self.instructions.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        row = QHBoxLayout()
        row.addWidget(self.status)
        row.addStretch()
        row.addWidget(self.button)
        layout.addLayout(row)
        layout.addWidget(self.instructions)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_expiry)
        self.reset()

    def reset(self):
        self._active = False
        self._timer.stop()
        self._set_state("idle", "When ModDB requests verification, click Verify in browser and complete the challenge. Downloads continue automatically.")

    def start(self):
        self._active = True

    def finish(self):
        self._active = False
        self._timer.stop()
        self.button.setEnabled(False)
        if self._state in {"required", "failed", "verifying"}:
            self.instructions.setText("This operation ended. Start it again to verify and continue from saved download progress.")
        self.status.setText("Cloudflare: Session ended")
        self._update_attention()

    def _update_attention(self):
        required = self._active and self._state in {"required", "failed"}
        self.setStyleSheet("#moddbAccess { background: #42331d; border: 1px solid #e9b45c; border-radius: 6px; }" if required else "")
        self.button.setText("Verify now in browser" if required else "Verify in browser")
        service = getattr(QApplication.instance(), "_desktop_notifications", None)
        if service is not None:
            service.set_verification_required(self, required)

    def _set_state(self, state, message, expires=None):
        labels = {"idle": "Not checked", "required": "Verification required", "verifying": "Verifying…",
                  "ready": "Cookie accepted", "not_needed": "No cookie needed", "failed": "Verification needed — retry"}
        if state not in labels:
            return
        self._state = state
        self._expires = expires if isinstance(expires, (int, float)) and expires > 0 else None
        self.status.setText(("⚠ " if state in {"required", "failed"} else "") + "Cloudflare: " + labels[state])
        self.instructions.setText(message)
        self.button.setEnabled(self._active and state in {"required", "failed"})
        self.status.setStyleSheet("color: #e9b45c;" if state in {"required", "failed"} else "")
        self._update_attention()
        if state == "ready" and self._expires:
            try:
                until = datetime.fromtimestamp(self._expires, timezone.utc).astimezone().strftime("%H:%M")
                self.status.setToolTip(f"Cookie expires at {until}. ModDB may request verification earlier. Cookie values stay private.")
            except (ValueError, OSError, OverflowError):
                self._expires = None
            self._timer.start()
        else:
            self.status.setToolTip("")
            self._timer.stop()

    def consume_line(self, line):
        if not line.startswith(ACCESS_PREFIX):
            return False
        try:
            event = json.loads(line[len(ACCESS_PREFIX):])
            if self._active and isinstance(event, dict) and isinstance(event.get("message"), str):
                self._set_state(event.get("state"), event["message"], event.get("expires"))
        except (ValueError, TypeError):
            pass
        return True

    def _update_expiry(self):
        if self._expires and time.time() >= self._expires:
            self._timer.stop()
            self.status.setText("Cloudflare: Cookie expired")
            self.instructions.setText("Existing transfers can continue. If ModDB rejects the next link request, use Verify in browser to renew access.")

    def _clicked(self):
        if self._active and self._state in {"required", "failed"} and self._verify():
            self._set_state("verifying", "Opening the browser… Complete the challenge; Commander will continue automatically.")
