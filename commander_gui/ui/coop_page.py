"""Guided GAMMA co-op setup, mode selection and session preparation."""
from __future__ import annotations

# import json
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    # QCheckBox,
    QFileDialog,
    # QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    # QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    # QSpinBox,
    QVBoxLayout,
    QWidget,
)

# from ..atomic import write_text
from ..config import cli_binary_path
from ..coop import (
    PROFILE,
    SLIM_MD5,
    SLIM_PAGE,
    SLIM_START,
    SLIM_VERSION,
    CoopError,
    CoopManager,
    # compare_manifests,
)
from ..coop_updates import NEWS_URL, check_coop_releases, version_key
from .common import (
    BackgroundTask,
    CommandRunner,
    # NoWheelComboBox,
    ProgressArea,
    StreamTask,
    clear_layout,
    info_label,
    make_card,
    make_header_row,
    open_url,
    section_label,
)

# Unvalidated UI integrations are commented out below for future development.
# Keep their widgets and callbacks out of the live page until validated.
# SESSION_SETTINGS_ENABLED = False
# MOD_COMPARISON_ENABLED = False
# ADOPTION_ENABLED = False


class CoopPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.task = None
        self.runner = None
        self._busy = False
        # self._session_key = None
        # self._session_baseline = None
        # self._session_drafts = {}
        self._pending_coop_profile = None
        self._release_task = None
        self._releases = []
        self._installed_version = None
        self._last_release_check = None
        self._release_error = ""
        self.buttons = []
        self.setObjectName("coopPage")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll)

        self._can_play = False
        self.install_card, self.install_layout = make_card()
        self.install_layout.setSpacing(12)
        self.badge = QLabel("Not installed")
        self.badge.setObjectName("statusOptional")
        self.install_layout.addLayout(make_header_row("xrRazom Co-op", self.badge))
        versions = QGridLayout()
        versions.setColumnStretch(0, 1)
        versions.setColumnStretch(1, 1)
        versions.addWidget(section_label("Installed", level=2), 0, 0)
        versions.addWidget(section_label("Latest announced", level=2), 0, 1, Qt.AlignmentFlag.AlignRight)
        self.installed_value = QLabel("Not installed")
        self.latest_value = QLabel("Not checked")
        for value in (self.installed_value, self.latest_value):
            value.setObjectName("modCounter")
            value.setTextFormat(Qt.TextFormat.PlainText)
        versions.addWidget(self.installed_value, 1, 0)
        versions.addWidget(self.latest_value, 1, 1, Qt.AlignmentFlag.AlignRight)
        self.install_layout.addLayout(versions)
        self.check_updates_button = QPushButton("Check for updates")
        self.check_updates_button.setObjectName("hero")
        self.check_updates_button.clicked.connect(self._check_updates)
        self.install_layout.addWidget(self.check_updates_button)
        self.release_status = info_label("")
        self.release_status.setTextFormat(Qt.TextFormat.PlainText)
        self.release_status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.install_layout.addWidget(self.release_status)
        self.last_checked = info_label("")
        self.last_checked.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.install_layout.addWidget(self.last_checked)
        self.status = info_label("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.install_layout.addWidget(self.status)
        self.install_hint = info_label(f"Slim {SLIM_VERSION} · GAMMA-compatible build")
        self.install_layout.addWidget(self.install_hint)
        install_actions = QHBoxLayout()
        self.download = self._button("Download and install", self._download, install_actions, style="primary")
        self.local_zip_button = self._button("Use a downloaded ZIP…", self._local_zip, install_actions)
        self._button("View release page", lambda: open_url(SLIM_PAGE), install_actions)
        self.install_layout.addLayout(install_actions)
        self.remove_button = self._button("Remove co-op", self._remove, install_actions, style="danger")
        self.remove_button.hide()
        root.addWidget(self.install_card)

        self.play_card, self.play_layout = make_card()
        self.play_layout.setSpacing(12)
        self.mode_value = QLabel("Single-player active")
        self.mode_value.setObjectName("statusOptional")
        self.play_layout.addLayout(make_header_row("Play co-op", self.mode_value))
        self.profile_hint = info_label("Co-op profile: " + PROFILE)
        self.profile_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.play_layout.addWidget(self.profile_hint)
        self.play_layout.addWidget(info_label(
            "Activate co-op to switch the engine and MO2 profile together. "
            "Close the game and MO2 before changing modes."
        ))
        modes = QHBoxLayout()
        self._button("Activate co-op", lambda: self._switch(True), modes)
        self._button("Switch to single-player", lambda: self._switch(False), modes)
        self.play_layout.addLayout(modes)
        instructions = QGridLayout()
        instructions.setHorizontalSpacing(24)
        instructions.setVerticalSpacing(8)
        instructions.setColumnStretch(0, 1)
        instructions.setColumnStretch(1, 1)
        instructions.addWidget(section_label("1. Set up in-game", level=2), 0, 0)
        instructions.addWidget(section_label("2. Connect with friends", level=2), 0, 1)
        instructions.addWidget(info_label(
            "Settings > xrRazom Co-op\n"
            "Set your player name, role and connection.\n"
            "Keep the same name to retain your identity on the host."
        ), 1, 0)
        instructions.addWidget(info_label(
            "Steam: start Steam; everyone needs Call of Pripyat.\n"
            "LAN / direct IP: close Steam.\n"
            "The host loads a co-op game; friends join from the main menu."
        ), 1, 1)
        self.play_layout.addLayout(instructions)
        self.play_layout.addWidget(info_label(
            "Use the same mods as your friends. Saves are shared by Anomaly's MO2 plugin; "
            "use separate save names for single-player and co-op."
        ))
        self.play_notice = info_label("")
        self.play_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.play_notice.setObjectName("warn")
        self.play_layout.addWidget(self.play_notice)
        self.play_button = self._button("Play co-op", self._play_coop, self.play_layout, style="hero")
        root.addWidget(self.play_card)

        self.progress = ProgressArea(self, show_table=False, auto_expand_log=False, resizable_log=True)
        self.progress.cancel_button.clicked.connect(self._cancel)
        self.install_layout.addWidget(self.progress)
        self.progress.hide()

        notes, notes_layout = make_card()
        source = QPushButton("View full changelogs on ModDB")
        source.setObjectName("githubLink")
        source.setFlat(True)
        source.clicked.connect(lambda: open_url(NEWS_URL))
        notes_layout.addLayout(make_header_row("What's New", source))
        self.notes_hint = info_label("Check for updates to load release summaries. Open a full changelog for all changes.")
        notes_layout.addWidget(self.notes_hint)
        self.notes_layout = QVBoxLayout()
        self.notes_layout.setContentsMargins(0, 0, 0, 0)
        notes_layout.addLayout(self.notes_layout)
        root.addWidget(notes)

        # friends, friends_layout = make_card()
        # friends_layout.setSpacing(12)
        # friends_layout.addLayout(make_header_row("Play with the same mods"))
        # friends_layout.addWidget(info_label(
            # "Share your session manifest, then compare it with your friend's to find differences in mods, load order and configs."
            # if MOD_COMPARISON_ENABLED else
            # "Temporarily disabled while compatibility checking is validated. Host and players must use the same mods; check mismatches in-game."
        # ))
        # self.friend_controls = QWidget()
        # self.friend_controls.setEnabled(MOD_COMPARISON_ENABLED)
        # friend_actions = QHBoxLayout()
        # self._button("Export manifest…", self._export, friend_actions)
        # self._button("Compare friend's manifest…", self._compare, friend_actions)
        # friend_actions.addStretch(1)
        # self.friend_controls.setLayout(friend_actions)
        # friends_layout.addWidget(self.friend_controls)
        # root.addWidget(friends)

        maintenance, maintenance_layout = make_card()
        self.maintenance_toggle = QPushButton("Show tools")
        self.maintenance_toggle.setObjectName("tertiary")
        self.maintenance_toggle.setCheckable(True)
        self.maintenance_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        maintenance_layout.addLayout(make_header_row("Maintenance and troubleshooting", self.maintenance_toggle))
        self.maintenance_body = QWidget()
        self.maintenance_body.setObjectName("panelTransparent")
        tools = QVBoxLayout(self.maintenance_body)
        self.maintenance_layout = tools
        tools.setContentsMargins(0, 8, 0, 0)
        tools.setSpacing(12)
        tools.addWidget(info_label("Restore co-op files or inspect a connection problem. Your co-op saves are kept."))
        recovery = QHBoxLayout()
        self._button("Repair co-op", self._repair, recovery)
        self._button("Recover interrupted setup", self._recover, recovery)
        recovery.addStretch(1)
        tools.addLayout(recovery)
        diagnostics = QHBoxLayout()
        self._button("Read connection mismatches", self._diagnostics, diagnostics)
        diagnostics.addStretch(1)
        tools.addLayout(diagnostics)
        maintenance_layout.addWidget(self.maintenance_body)
        self.maintenance_body.hide()
        self.maintenance_toggle.toggled.connect(self._toggle_maintenance)
        root.addWidget(maintenance)
        # Unvalidated UI retained for future development.
        # setup_layout.addWidget(info_label("Already have co-op installed?"))
        # self.adopt = QCheckBox("Adopt an existing co-op setup")
        # self.adopt.setToolTip("Preserve the existing co-op profile settings and back up replaced mod files.")
        # if not ADOPTION_ENABLED:
            # self.adopt.setText("Adopt an existing co-op setup (unavailable)")
            # self.adopt.setToolTip("Temporarily unavailable: adopting existing setups has not been validated.")
        # setup_layout.addWidget(self.adopt)
        # if not SESSION_SETTINGS_ENABLED:
            # session_layout.addWidget(info_label("Launcher session controls are temporarily disabled; configure these settings in-game."))
        # self.session_controls = QWidget()
        # self.session_controls.setEnabled(SESSION_SETTINGS_ENABLED)
        # self.session_toggle = QPushButton("Show unavailable controls")
        # self.session_toggle.setObjectName("tertiary")
        # self.session_toggle.setCheckable(True)
        # self.session_toggle.setVisible(not SESSION_SETTINGS_ENABLED)
        # session_layout.addWidget(self.session_toggle)
        # session_layout.addWidget(self.session_controls)
        # self.session_controls.setVisible(SESSION_SETTINGS_ENABLED)
        # self.session_toggle.toggled.connect(self._toggle_session_controls)
        # session_layout = QVBoxLayout(self.session_controls)
        # session_layout.setContentsMargins(0, 0, 0, 0)
        # session_layout.setSpacing(12)
        # session_layout.addWidget(QLabel("Player name"))
        # self.name = QLineEdit()
        # self.name.setMaxLength(48)
        # self.name.setMinimumHeight(34)
        # self.name.setPlaceholderText("Your name in the session")
        # session_layout.addWidget(self.name)
        # session_layout.addWidget(info_label("Keep this name to retain your identity on the host."))

        # connection = QGridLayout()
        # connection.setHorizontalSpacing(16)
        # connection.setVerticalSpacing(8)
        # connection.setColumnStretch(0, 1)
        # connection.setColumnStretch(1, 1)
        # connection.addWidget(QLabel("Role"), 0, 0)
        # connection.addWidget(QLabel("Connection"), 0, 1)
        # self.role = NoWheelComboBox()
        # self.role.addItems(["Join a friend", "Host a session"])
        # self.transport = NoWheelComboBox()
        # self.transport.addItems(["Steam", "LAN / direct IP"])
        # for widget in (self.role, self.transport):
            # widget.setMinimumHeight(34)
        # connection.addWidget(self.role, 1, 0)
        # connection.addWidget(self.transport, 1, 1)
        # session_layout.addLayout(connection)

        # self.connection_fields = QFormLayout()
        # self.connection_fields.setSpacing(12)
        # self.connection_fields.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        # self.address = QLineEdit()
        # self.address.setMinimumHeight(34)
        # self.address.setPlaceholderText("e.g. 192.168.1.10")
        # self.connection_fields.addRow("Host address", self.address)
        # self.port = QSpinBox()
        # self.port.setRange(1, 65535)
        # self.port.setValue(5445)
        # self.connection_fields.addRow("Host LAN port", self.port)
        # self.players = QSpinBox()
        # self.players.setRange(2, 4)
        # self.players.setValue(4)
        # self.connection_fields.addRow("Player limit", self.players)
        # session_layout.addLayout(self.connection_fields)
        # session_layout.addWidget(section_label("Before you launch", level=2))
        # self.instructions = info_label("")
        # session_layout.addWidget(self.instructions)
        # session_layout.addSpacing(4)
        # self.launch_button = self._button("Launch co-op", self._launch, session_layout, style="hero")
        # self._button("Save session settings", self._save, session_layout)
        # if SESSION_SETTINGS_ENABLED:
            # session_layout.addWidget(info_label("Launch saves these settings and activates co-op automatically."))
        root.addStretch(1)
        self.refresh()
        self.on_busy_changed(self.window.install_busy)
        if getattr(window, "coop_updates", None) is not None:
            window.coop_updates.changed.connect(self._sync_release_check)
            self._sync_release_check()

    def _button(self, text, callback, row, *, style=""):
        button = QPushButton(text)
        if style:
            button.setObjectName(style)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        row.addWidget(button)
        self.buttons.append(button)
        return button

    def _toggle_maintenance(self, expanded):
        self.maintenance_body.setVisible(expanded)
        self.maintenance_toggle.setText("Hide tools" if expanded else "Show tools")

    # def _toggle_session_controls(self, expanded):
        # self.session_controls.setVisible(expanded)
        # self.session_toggle.setText("Hide unavailable controls" if expanded else "Show unavailable controls")

    def _manager(self):
        profile = self.window.settings.active_profile
        if profile is None:
            raise CoopError("Select an installed GAMMA profile in Settings first.")
        return CoopManager(profile)

    def refresh(self):
        if self._busy:
            return
        self._can_play = False
        self.play_notice.clear()
        self.download.show()
        self.local_zip_button.show()
        self.remove_button.hide()
        try:
            manager = self._manager()
            state = manager.state()
            self._installed_version = state.get("version") if state.get("installed") else None
            installed = bool(state.get("installed"))
            self.download.setVisible(not installed)
            self.local_zip_button.setVisible(not installed)
            self.remove_button.setVisible(installed)
            if manager.journal_path.exists():
                text = "Interrupted setup — use Recover interrupted setup before playing."
                badge, style, mode = "Recovery needed", "statusNotReady", "Setup interrupted"
                self.maintenance_toggle.setChecked(True)
            elif state.get("maintenance"):
                text = "GAMMA update interrupted — finish or repair that update before playing."
                badge, style, mode = "Update incomplete", "statusNotReady", "Update required"
            elif state.get("installed"):
                text = f"xrRazom {state['version']} installed · " + ("Co-op engine active" if state.get("active") else "Single-player engine active")
                mode = "Co-op active" if state.get("active") else "Single-player active"
                badge, style = "Installed", "statusReady"
                self._can_play = bool(state.get("active"))
                try:
                    manager.validate_coop_profile(state)
                except (OSError, ValueError, CoopError) as exc:
                    self._can_play = False
                    self.play_notice.setText(str(exc))
            else:
                text = f"Not installed · setup will create a Co-op profile from {manager.profile.singleplayer_profile}."
                badge, style, mode = "Not installed", "statusOptional", "Single-player"
            self._set_badge(badge, style)
            self.mode_value.setText(mode)
            self.mode_value.setObjectName("statusReady" if self._can_play else "statusOptional")
            self.mode_value.style().unpolish(self.mode_value)
            self.mode_value.style().polish(self.mode_value)
            self.profile_hint.setText("Single-player: " + manager.profile.singleplayer_profile + "   ·   Co-op: " + (manager.profile.mo2_coop_profile or "Not created"))
            self.status.setText(text)
            # self._refresh_session(manager)
        except (OSError, ValueError, CoopError) as exc:
            self._installed_version = None
            self.status.setText(str(exc))
            self._set_badge("Needs attention", "statusNotReady")
            self.mode_value.setText("Needs attention")
            self.play_notice.setText(str(exc))
        # self._instructions()
        self._render_release_status()
        self.play_notice.setVisible(bool(self.play_notice.text()))
        self._update_play_button()

    def _render_release_status(self):
        latest = self._releases[0].version if self._releases else "Not checked"
        installed = self._installed_version or "Not installed"
        self.installed_value.setText(installed)
        self.latest_value.setText(latest)
        if self._release_task is not None:
            message = "Checking xrRazom releases…"
        elif self._release_error:
            message = "Update check failed: " + self._release_error
        elif not self._releases:
            message = "Check for new xrRazom release announcements."
        else:
            latest_key = version_key(latest)
            try:
                installed_key = version_key(self._installed_version) if self._installed_version else None
            except ValueError:
                installed_key = None
            if installed_key is None:
                message = f"xrRazom {latest} has been announced."
            elif latest_key > installed_key:
                message = f"Update available: xrRazom {latest}."
            elif latest_key == installed_key:
                message = "You have the latest announced xrRazom release."
            else:
                message = "Your installed version is newer than the release feed."
            if latest_key > version_key(SLIM_VERSION):
                message += f" Commander currently installs Slim {SLIM_VERSION}; update Commander when support for this release is available."
            elif installed_key is not None and latest_key > installed_key:
                message += " See the release page for upgrade instructions."
        self.release_status.setText(message)
        self.last_checked.setText("Last successful check: " + self._last_release_check.strftime("%Y-%m-%d %H:%M") if self._last_release_check else "")

    def _sync_release_check(self):
        service = self.window.coop_updates
        if service.releases and service.releases is not self._releases:
            self._releases_loaded(service.releases)
        self._release_task = service.task
        self._release_error = service.error
        self._last_release_check = service.checked_at
        self.check_updates_button.setEnabled(service.task is None)
        self._render_release_status()

    def _check_updates(self):
        if getattr(self.window, "coop_updates", None) is not None:
            self.window.coop_updates.check()
            return
        if self._release_task is not None:
            return
        self._release_error = ""
        self.check_updates_button.setEnabled(False)
        self._release_task = BackgroundTask(check_coop_releases, parent=self)
        self._release_task.result.connect(self._releases_loaded)
        self._release_task.error.connect(self._releases_failed)
        self._render_release_status()
        self._release_task.start()

    def _releases_loaded(self, releases):
        from .update_page import _ReleaseNotesSection

        self._release_task = None
        self._releases = releases
        self._release_error = ""
        self._last_release_check = datetime.now(timezone.utc).astimezone()
        self.check_updates_button.setEnabled(True)
        self.notes_hint.setText("Release summaries from xrRazom's official news feed. Open a full changelog for all changes.")
        clear_layout(self.notes_layout)
        for index, release in enumerate(releases):
            section = _ReleaseNotesSection(release.title, "", expanded=index == 0)
            section.body.setPlainText(release.summary or "Open the full changelog to read this release's changes.")
            section.body.setFixedHeight(140)
            link = QPushButton("Read full changelog" + (f" · {release.date}" if release.date else ""))
            link.setObjectName("githubLink")
            link.setFlat(True)
            link.clicked.connect(lambda checked=False, url=release.url: open_url(url))
            link.setVisible(index == 0)
            section.toggle_button.toggled.connect(link.setVisible)
            section.layout().addWidget(link)
            self.notes_layout.addWidget(section)
        self._render_release_status()

    def _releases_failed(self, message):
        self._release_task = None
        self._release_error = message
        self.check_updates_button.setEnabled(True)
        self._render_release_status()

    # def _refresh_session(self, manager):
        # # Session settings belong to an installation, shared by its solo/co-op
        # # profiles. Refresh status without replacing edits with the saved values.
        # key = (str(manager.gamma), str(manager.anomaly))
        # values = manager.options()
        # saved = (values.get("name", ""), bool(values.get("host", False)), bool(values.get("steam", True)),
                 # values.get("port", 5445), values.get("players", 4), values.get("address", ""))
        # if self._session_key == key:
            # baseline, draft = self._session_baseline, self._options()
        # else:
            # if self._session_key is not None:
                # self._session_drafts[self._session_key] = (self._session_baseline, self._options())
            # baseline, draft = self._session_drafts.pop(key, (saved, saved))
        # merged = tuple(edit if edit != old else new for edit, old, new in zip(draft, baseline, saved))
        # self._session_key, self._session_baseline = key, saved
        # name, host, steam, port, players, address = merged
        # # Avoid disturbing cursor positions or selections in the text fields.
        # if self.name.text() != name:
            # self.name.setText(name)
        # if self.address.text() != address:
            # self.address.setText(address)
        # self.role.setCurrentIndex(int(host))
        # self.transport.setCurrentIndex(0 if steam else 1)
        # self.port.setValue(port)
        # self.players.setValue(players)

    # def _session_saved(self, message=None):
        # # Called only after saving succeeds; accept normalization and allow
        # # future refreshes to pick up changes made in the game.
        # self._session_baseline = self._options()
        # self.refresh()
        # if isinstance(message, str):
            # self.status.setText(message)

    def _set_badge(self, text, style):
        self.badge.setText(text)
        if self.badge.objectName() != style:
            self.badge.setObjectName(style)
            self.badge.style().unpolish(self.badge)
            self.badge.style().polish(self.badge)

    # def _instructions(self):
        # steam = self.transport.currentIndex() == 0
        # host = self.role.currentIndex() == 1
        # self.connection_fields.setRowVisible(self.address, not steam and not host)
        # self.connection_fields.setRowVisible(self.port, not steam and host)
        # self.connection_fields.setRowVisible(self.players, host)
        # self.address.setEnabled(not steam and not host and not self._busy)
        # self.port.setEnabled(host and not steam and not self._busy)
        # self.players.setEnabled(host and not self._busy)
        # text = ("Everyone must start Steam and own Call of Pripyat. " if steam else "Everyone must close Steam before launching. ")
        # text += ("Start a new game or load a co-op save to host." if host else
                 # "At the main menu, join your friend through the Steam overlay." if steam else
                 # "At the main menu, choose Join Session and confirm the host IP.")
        # self.instructions.setText(text)

    def on_busy_changed(self, busy):
        for button in self.buttons:
            button.setEnabled(not busy)
        self._update_play_button()
        # self.adopt.setEnabled(not busy and ADOPTION_ENABLED)
        # for widget in (self.name, self.role, self.transport, self.address, self.port, self.players):
            # widget.setEnabled(not busy)
        # if not busy:
            # self._instructions()

    def _update_play_button(self):
        self.play_button.setEnabled(self._can_play and not self.window.install_busy and not self._busy)
        self.play_button.setToolTip("Launch through MO2 using the Play tab's selected executable." if self._can_play else
                                   "Install and activate co-op before playing.")

    def _play_coop(self):
        if self.window.install_busy or self._busy:
            return
        self.refresh()
        if not self._can_play:
            return
        try:
            # Selecting the managed profile never activates or replaces binaries.
            manager = self._manager()
            name = manager.validate_coop_profile()
            self._select(name)
            manager.assert_launch(verify_files=False)
            self.window.set_page("play")
            page = self.window._ensure_page("play")
            page.refresh()
            page.launch_game()  # Reuses MO2 resolution, full engine checks and process monitoring.
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Co-op", str(exc))

    def _begin(self, area="maintenance"):
        if self.window.install_busy or self._busy:
            return False
        self._busy = True
        self.window.set_install_busy(True, operation="coop")
        self.progress.reset()
        layout = {"install": self.install_layout, "play": self.play_layout,
                  "maintenance": self.maintenance_layout}[area]
        if area == "maintenance":
            self.maintenance_toggle.setChecked(True)
        layout.addWidget(self.progress)
        self.progress.show()
        QTimer.singleShot(0, self._reveal_activity)
        return True

    def _reveal_activity(self):
        self.scroll.ensureWidgetVisible(self.progress)

    def _end(self, message=""):
        self._busy = False
        self.runner = None
        self.progress.hide()
        self.window.set_install_busy(False)
        self.refresh()
        if message:
            self.status.setText(message)

    def _error(self, message):
        self.progress.on_finished(1, str(message))
        self._end(str(message))
        QMessageBox.warning(self, "Co-op", str(message))

    def _work(self, fn, done=None, *, already_busy=False, cancelable=False, area="maintenance"):
        if not already_busy and not self._begin(area):
            return
        self.task = StreamTask(fn, parent=self)
        self.progress.bar.setRange(0, 0)
        self.progress.bar.setFormat("Working…")
        self.progress.cancel_button.setVisible(cancelable)
        self.progress.cancel_button.setEnabled(True)
        self.progress.cancel_button.setText("Cancel")
        self.progress.pause_button.hide()
        self.task.line.connect(self.progress.on_line)
        self.task.error.connect(self._error)
        def finished(result):
            self.progress.on_finished(0, str(result) if isinstance(result, str) else "Done")
            self._end()
            try:
                if done:
                    done(result)
                elif isinstance(result, str):
                    self.status.setText(result)
            except (OSError, ValueError, RuntimeError) as exc:
                self._error(str(exc))
        self.task.result.connect(finished)
        self.task.start()

    def _cancel(self):
        if self.runner is not None:
            self.runner.cancel()
        elif self.task is not None:
            self.task.cancel()
        self.progress.cancel_button.setEnabled(False)
        self.progress.status_message("Stopping; downloaded data will be kept…")

    def _select(self, name):
        profile = self.window.settings.active_profile
        profile.select_mo2_profile(name)
        self.window.settings.save()
        self.window.refresh_settings()
        self.refresh()

    def _prepare_coop_profile(self):
        try:
            manager = self._manager()
            state = manager.state()
            if state.get("installed"):
                self._pending_coop_profile = manager.validate_coop_profile(state, require_mod=False)
                return True
            source = manager.validate_profile_name(manager.profile.singleplayer_profile)
            if not (manager.gamma / "profiles" / source / "modlist.txt").is_file():
                raise CoopError("Choose an installed single-player MO2 profile in Profiles before installing co-op.")
            default = manager.profile.mo2_coop_profile or (PROFILE if source == "G.A.M.M.A" else source + " Co-op")
            while True:
                name, accepted = QInputDialog.getText(
                    self, "Create your Co-op profile",
                    f"Commander will copy the mod selection and load order from {source}.\n"
                    "xrRazom will be enabled in the new profile at highest priority.\n"
                    "Your single-player mod list stays unchanged. Anomaly saves are shared.\n\n"
                    "Name for the Co-op MO2 profile:", QLineEdit.EchoMode.Normal, default)
                if not accepted:
                    return False
                name = name.strip()
                try:
                    manager.validate_profile_name(name)
                    if name.casefold() == source.casefold():
                        raise CoopError("Use a different name from your single-player profile.")
                    destination = manager.gamma / "profiles" / name
                    managed = state.get("source_profile") and name.casefold() == manager.coop_profile_name(state).casefold()
                    if destination.exists() and not managed:
                        raise CoopError("That MO2 profile already exists. Choose a new name so it is not overwritten.")
                    self._pending_coop_profile = name
                    return True
                except CoopError as exc:
                    QMessageBox.warning(self, "Co-op profile", str(exc))
                    default = name
        except (OSError, ValueError, CoopError) as exc:
            QMessageBox.warning(self, "Co-op profile", str(exc))
            return False

    def _local_zip(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select official xrRazom Slim ZIP", "", "ZIP archives (*.zip)")
        if path and self._prepare_coop_profile():
            self._install(Path(path))

    def _install(self, archive, *, already_busy=False):
        try:
            manager = self._manager()
            # adopt = ADOPTION_ENABLED and self.adopt.isChecked()
            adopt = False
            self._work(lambda report: manager.install_zip(archive, coop_profile=self._pending_coop_profile, adopt=adopt, report=report, cancel=self.task.cancel_event),
                       lambda _: self._select(manager.profile.mo2_coop_profile), already_busy=already_busy, cancelable=True, area="install")
        except (OSError, ValueError, RuntimeError) as exc:
            self._error(str(exc))

    def _download(self):
        if not self._prepare_coop_profile():
            return
        try:
            manager = self._manager()
            archive = manager.root / "downloads" / f"xrRazom-{SLIM_VERSION}-Slim.zip"
            if not self._begin("install"):
                return
            self.runner = CommandRunner([str(cli_binary_path()), "addon", "download", "--url", SLIM_START,
                                         "--output", str(archive), "--md5", SLIM_MD5], parent=self)
            self.progress.set_runner(self.runner)
            self.runner.line.connect(self.progress.on_line)
            self.runner.finished.connect(lambda rc, output: self._download_finished(rc, output, archive))
            self.progress.on_started()
            self.runner.start()
        except (OSError, ValueError, RuntimeError) as exc:
            self._error(str(exc))

    def _download_finished(self, rc, output, archive):
        self.progress.on_finished(rc, output)
        self.progress.set_runner(None)
        cancelled = self.runner and self.runner.was_cancelled
        self.runner = None
        if rc or cancelled:
            self._end("Download stopped. Download & install resumes the saved partial archive.")
        else:
            self._install(archive, already_busy=True)

    def _switch(self, active):
        try:
            manager = self._manager()
            self._work(lambda _: manager.switch(active), self._select, area="play")
        except (OSError, ValueError, RuntimeError) as exc:
            self._error(str(exc))

    # def _options(self):
        # return (self.name.text(), self.role.currentIndex() == 1, self.transport.currentIndex() == 0,
                # self.port.value(), self.players.value(), self.address.text())

    # def _save(self):
        # if not SESSION_SETTINGS_ENABLED:
            # return
        # try:
            # manager, options = self._manager(), self._options()
            # self._work(lambda _: manager.save_options(*options), self._session_saved)
        # except (OSError, ValueError, RuntimeError) as exc:
            # self._error(str(exc))

    # def _launch(self):
        # if not SESSION_SETTINGS_ENABLED:
            # return
        # try:
            # from ..windows import executable_pids
            # manager, options = self._manager(), self._options()
            # steam_running = bool(executable_pids("steam.exe"))
            # if options[2] != steam_running:
                # raise CoopError("Start Steam for a Steam session." if options[2] else "Close Steam for a LAN session.")
            # def prepare(_):
                # manager.save_options(*options)
                # return manager.switch(True)
            # def launch(name):
                # self._session_saved()
                # self._select(name)
                # self.window.set_page("play")
                # page = self.window._ensure_page("play")
                # page.refresh()
                # page.launch_game()
            # self._work(prepare, launch)
        # except (OSError, ValueError, RuntimeError) as exc:
            # self._error(str(exc))

    def _recover(self):
        try:
            manager = self._manager()
            self._work(lambda _: manager.recover())
        except (OSError, ValueError, RuntimeError) as exc:
            self._error(str(exc))

    def _repair(self):
        try:
            manager = self._manager()
            self._work(lambda _: manager.repair())
        except (OSError, ValueError, RuntimeError) as exc:
            self._error(str(exc))

    def _remove(self):
        if QMessageBox.question(self, "Remove co-op", "Restore single-player engine files and remove the managed co-op payload? Your co-op profile and saves will be kept.") != QMessageBox.StandardButton.Yes:
            return
        try:
            manager = self._manager()
            self._work(lambda _: manager.uninstall(), self._select)
        except (OSError, ValueError, RuntimeError) as exc:
            self._error(str(exc))

    # def _export(self):
        # if not MOD_COMPARISON_ENABLED:
            # return
        # path, _ = QFileDialog.getSaveFileName(self, "Export co-op session manifest", "gamma-coop.json", "JSON (*.json)")
        # if path:
            # try:
                # manager = self._manager()
                # def export(report):
                    # write_text(Path(path), json.dumps(manager.manifest(report), indent=2))
                    # return "Manifest exported. Share it with your friends; it contains mod names and config hashes, not saves or personal paths."
                # self._work(export)
            # except (OSError, ValueError, RuntimeError) as exc:
                # self._error(str(exc))

    # def _compare(self):
        # if not MOD_COMPARISON_ENABLED:
            # return
        # path, _ = QFileDialog.getOpenFileName(self, "Compare friend's manifest", "", "JSON (*.json)")
        # if path:
            # try:
                # manager = self._manager()
                # def compare(report):
                    # friend = json.loads(Path(path).read_text(encoding="utf-8"))
                    # differences = compare_manifests(manager.manifest(report), friend)
                    # return "\n".join(differences) or "Mod order and checked configs match. The game's connection check is still authoritative."
                # self._work(compare, lambda result: QMessageBox.information(self, "Session comparison", result))
            # except (OSError, ValueError, RuntimeError) as exc:
                # self._error(str(exc))

    def _diagnostics(self):
        try:
            manager = self._manager()
            logs = sorted((manager.anomaly / "appdata" / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            lines = logs[0].read_text(encoding="utf-8", errors="replace").splitlines() if logs else []
            mismatches = [line for line in lines if "[xrRazom]" in line and "cfg-diff" in line]
            QMessageBox.information(self, "Connection mismatches", "\n".join(mismatches[-80:]) or "No xrRazom cfg-diff entries in the newest game log.")
        except (OSError, ValueError, RuntimeError) as exc:
            self._error(str(exc))
