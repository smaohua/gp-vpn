#!/usr/bin/env python3
"""
OpenConnect GUI — a small tray application for managing openconnect VPN
connections (GlobalProtect / AnyConnect / etc.) on Linux.

Features
  * Store multiple connection profiles (protocol, gateway, port, user, args).
  * Connect / disconnect from a window or from the system-tray icon.
  * Tray icon reflects connection state (grey = off, amber = connecting,
    green = connected).
  * Elevates with `sudo -S`; your sudo password is asked in-app and can be
    remembered for the session or saved.
  * Secrets are stored in ~/.config/openconnect-gui/secrets.json with 0600
    permissions (or the system keyring if `keyring` is installed).

This reproduces a command like:
    sudo openconnect --protocol=gp --user=phs02 pa-gzsite.jxsdwan.com:8443
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QProcess, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QBrush, QPen, QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialogButtonBox, QDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Optional system keyring support
# ---------------------------------------------------------------------------
try:
    import keyring  # type: ignore
    _HAVE_KEYRING = True
except Exception:  # pragma: no cover - depends on environment
    keyring = None
    _HAVE_KEYRING = False

APP_NAME = "openconnect-gui"
APP_TITLE = "OpenConnect VPN"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
SECRETS_FILE = CONFIG_DIR / "secrets.json"
RUN_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / APP_NAME

PROTOCOLS = ["gp", "anyconnect", "nc", "pulse", "f5", "fortinet", "array"]

# Lines openconnect prints once the tunnel is actually up.
_CONNECTED_PATTERNS = [
    re.compile(r"Connected as ", re.I),
    re.compile(r"ESP session established", re.I),
    re.compile(r"Established DTLS connection", re.I),
    re.compile(r"Configured as ", re.I),
    re.compile(r"Connected tun\d", re.I),
]
# Untrusted-certificate confirmation prompt (no TTY -> we must answer it).
_CERT_PROMPT = re.compile(r"(to accept|enter 'yes'|\(y/n\)|connect anyway)", re.I)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Profile:
    name: str = "New profile"
    protocol: str = "gp"
    gateway: str = ""
    port: int = 443
    username: str = ""
    extra_args: str = ""
    save_vpn_password: bool = False
    auto_accept_cert: bool = False

    def server(self) -> str:
        if self.port and self.port not in (0, 443):
            return f"{self.gateway}:{self.port}"
        if self.port == 443:
            return f"{self.gateway}:443"
        return self.gateway


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
class Store:
    """Profiles in config.json; passwords in keyring or 0600 secrets.json."""

    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass
        self.profiles: list[Profile] = []
        self.last_selected: int = 0
        self.remember_sudo: bool = False
        self._secrets: dict[str, str] = {}
        self.load()

    # -- profiles -----------------------------------------------------------
    def load(self) -> None:
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                self.profiles = [Profile(**p) for p in data.get("profiles", [])]
                self.last_selected = data.get("last_selected", 0)
                self.remember_sudo = data.get("remember_sudo", False)
            except Exception as exc:  # corrupt config shouldn't kill the app
                print(f"warning: could not read config: {exc}", file=sys.stderr)
        if not self.profiles:
            self.profiles = [Profile()]
        if not _HAVE_KEYRING and SECRETS_FILE.exists():
            try:
                self._secrets = json.loads(SECRETS_FILE.read_text())
            except Exception:
                self._secrets = {}

    def save(self) -> None:
        data = {
            "profiles": [asdict(p) for p in self.profiles],
            "last_selected": self.last_selected,
            "remember_sudo": self.remember_sudo,
        }
        CONFIG_FILE.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass

    # -- secrets ------------------------------------------------------------
    def _flush_secrets(self) -> None:
        if _HAVE_KEYRING:
            return
        SECRETS_FILE.write_text(json.dumps(self._secrets))
        try:
            os.chmod(SECRETS_FILE, 0o600)
        except OSError:
            pass

    def get_secret(self, key: str) -> Optional[str]:
        if _HAVE_KEYRING:
            try:
                return keyring.get_password(APP_NAME, key)
            except Exception:
                return None
        return self._secrets.get(key)

    def set_secret(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self.del_secret(key)
            return
        if _HAVE_KEYRING:
            try:
                keyring.set_password(APP_NAME, key, value)
            except Exception as exc:
                print(f"warning: keyring write failed: {exc}", file=sys.stderr)
            return
        self._secrets[key] = value
        self._flush_secrets()

    def del_secret(self, key: str) -> None:
        if _HAVE_KEYRING:
            try:
                keyring.delete_password(APP_NAME, key)
            except Exception:
                pass
            return
        if key in self._secrets:
            del self._secrets[key]
            self._flush_secrets()


# ---------------------------------------------------------------------------
# Icons (drawn at runtime so we don't ship image files)
# ---------------------------------------------------------------------------
def make_icon(color: QColor) -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(color))
    p.setPen(QPen(QColor(30, 30, 30), 3))
    p.drawEllipse(6, 6, 52, 52)
    p.setPen(QPen(QColor(255, 255, 255)))
    f = QFont("Sans", 26, QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "V")
    p.end()
    return QIcon(pix)


COLOR_OFF = QColor(120, 120, 120)
COLOR_CONNECTING = QColor(230, 170, 30)
COLOR_ON = QColor(40, 180, 70)


# ---------------------------------------------------------------------------
# Sudo password dialog
# ---------------------------------------------------------------------------
class SudoDialog(QDialog):
    def __init__(self, parent=None, remember_default=False):
        super().__init__(parent)
        self.setWindowTitle("Administrator password")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Enter your sudo password to start the VPN:"))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.pw)
        self.remember = QCheckBox("Remember for this session")
        self.remember.setChecked(remember_default)
        layout.addWidget(self.remember)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.pw.setFocus()

    @property
    def password(self) -> str:
        return self.pw.text()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    STATE_OFF = "disconnected"
    STATE_CONNECTING = "connecting"
    STATE_ON = "connected"
    STATE_DISCONNECTING = "disconnecting"

    def __init__(self) -> None:
        super().__init__()
        self.store = Store()
        self.state = self.STATE_OFF
        self.proc: Optional[QProcess] = None
        self._sudo_pw: Optional[str] = None
        self._active_profile: Optional[Profile] = None
        self._explicit_quit = False
        self._warned_hide = False

        RUN_DIR.mkdir(parents=True, exist_ok=True)

        self.setWindowTitle(APP_TITLE)
        self.resize(560, 620)
        self._build_ui()
        self._build_tray()
        self._load_into_form(self._current_profile())
        self._set_state(self.STATE_OFF)

    # -- UI construction ----------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Profile selector row
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        sel_row.addWidget(self.profile_combo, 1)
        self.btn_new = QPushButton("New")
        self.btn_new.clicked.connect(self._new_profile)
        self.btn_del = QPushButton("Delete")
        self.btn_del.clicked.connect(self._delete_profile)
        sel_row.addWidget(self.btn_new)
        sel_row.addWidget(self.btn_del)
        root.addLayout(sel_row)

        # Profile fields
        box = QGroupBox("Connection settings")
        form = QFormLayout(box)
        self.f_name = QLineEdit()
        self.f_protocol = QComboBox()
        self.f_protocol.addItems(PROTOCOLS)
        self.f_protocol.setEditable(True)
        self.f_gateway = QLineEdit()
        self.f_gateway.setPlaceholderText("pa-gzsite.jxsdwan.com")
        self.f_port = QSpinBox()
        self.f_port.setRange(1, 65535)
        self.f_port.setValue(443)
        self.f_user = QLineEdit()
        self.f_user.setPlaceholderText("phs02")
        self.f_vpn_pw = QLineEdit()
        self.f_vpn_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.f_save_vpn = QCheckBox("Save VPN password")
        self.f_extra = QLineEdit()
        self.f_extra.setPlaceholderText("--os=linux  --servercert pin-sha256:...")
        self.f_autocert = QCheckBox("Auto-accept server certificate (insecure)")

        form.addRow("Name", self.f_name)
        form.addRow("Protocol", self.f_protocol)
        form.addRow("Gateway", self.f_gateway)
        form.addRow("Port", self.f_port)
        form.addRow("Username", self.f_user)
        form.addRow("VPN password", self.f_vpn_pw)
        form.addRow("", self.f_save_vpn)
        form.addRow("Extra args", self.f_extra)
        form.addRow("", self.f_autocert)
        root.addWidget(box)

        # Action buttons
        act_row = QHBoxLayout()
        self.btn_save = QPushButton("Save profile")
        self.btn_save.clicked.connect(self._save_current)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._connect)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.clicked.connect(self._disconnect)
        act_row.addWidget(self.btn_save)
        act_row.addStretch(1)
        act_row.addWidget(self.btn_connect)
        act_row.addWidget(self.btn_disconnect)
        root.addLayout(act_row)

        # Status
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self.status_label)

        # Log
        root.addWidget(QLabel("Log:"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        font = QFont("Monospace")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.log.setFont(font)
        root.addWidget(self.log, 1)

        self._reload_combo()

    def _build_tray(self) -> None:
        self.icon_off = make_icon(COLOR_OFF)
        self.icon_connecting = make_icon(COLOR_CONNECTING)
        self.icon_on = make_icon(COLOR_ON)

        self.tray = QSystemTrayIcon(self.icon_off, self)
        self.tray.setToolTip(APP_TITLE)
        # Parent the menu and every action to ``self`` so Qt keeps them alive;
        # otherwise locally-scoped actions get garbage-collected and silently
        # disappear from the tray menu after this method returns.
        self.tray_menu = QMenu(self)
        self.act_status = QAction("Disconnected", self)
        self.act_status.setEnabled(False)
        self.tray_menu.addAction(self.act_status)
        self.tray_menu.addSeparator()
        self.act_connect = QAction("Connect", self)
        self.act_connect.triggered.connect(self._connect)
        self.tray_menu.addAction(self.act_connect)
        self.act_disconnect = QAction("Disconnect", self)
        self.act_disconnect.triggered.connect(self._disconnect)
        self.tray_menu.addAction(self.act_disconnect)
        self.tray_menu.addSeparator()
        self.act_show = QAction("Show window", self)
        self.act_show.triggered.connect(self._show_window)
        self.tray_menu.addAction(self.act_show)
        self.act_quit = QAction("Quit", self)
        self.act_quit.triggered.connect(self._quit)
        self.tray_menu.addAction(self.act_quit)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    # -- profile <-> form ---------------------------------------------------
    def _reload_combo(self) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems([p.name for p in self.store.profiles])
        idx = min(self.store.last_selected, len(self.store.profiles) - 1)
        self.profile_combo.setCurrentIndex(max(0, idx))
        self.profile_combo.blockSignals(False)

    def _current_profile(self) -> Profile:
        idx = self.profile_combo.currentIndex()
        if 0 <= idx < len(self.store.profiles):
            return self.store.profiles[idx]
        return self.store.profiles[0]

    def _load_into_form(self, p: Profile) -> None:
        self.f_name.setText(p.name)
        self.f_protocol.setCurrentText(p.protocol)
        self.f_gateway.setText(p.gateway)
        self.f_port.setValue(p.port or 443)
        self.f_user.setText(p.username)
        self.f_extra.setText(p.extra_args)
        self.f_save_vpn.setChecked(p.save_vpn_password)
        self.f_autocert.setChecked(p.auto_accept_cert)
        key = self._vpn_key(p)
        self.f_vpn_pw.setText(self.store.get_secret(key) or "" if p.save_vpn_password else "")

    def _form_into_profile(self, p: Profile) -> None:
        p.name = self.f_name.text().strip() or "Unnamed"
        p.protocol = self.f_protocol.currentText().strip()
        p.gateway = self.f_gateway.text().strip()
        p.port = self.f_port.value()
        p.username = self.f_user.text().strip()
        p.extra_args = self.f_extra.text().strip()
        p.save_vpn_password = self.f_save_vpn.isChecked()
        p.auto_accept_cert = self.f_autocert.isChecked()

    @staticmethod
    def _vpn_key(p: Profile) -> str:
        return f"vpn::{p.protocol}::{p.username}@{p.gateway}"

    # -- profile actions ----------------------------------------------------
    def _on_profile_changed(self, idx: int) -> None:
        if 0 <= idx < len(self.store.profiles):
            self.store.last_selected = idx
            self._load_into_form(self.store.profiles[idx])

    def _new_profile(self) -> None:
        self.store.profiles.append(Profile())
        self.store.last_selected = len(self.store.profiles) - 1
        self._reload_combo()
        self._load_into_form(self._current_profile())

    def _delete_profile(self) -> None:
        if len(self.store.profiles) <= 1:
            QMessageBox.information(self, APP_TITLE, "At least one profile is required.")
            return
        idx = self.profile_combo.currentIndex()
        name = self.store.profiles[idx].name
        if QMessageBox.question(self, APP_TITLE, f"Delete profile '{name}'?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.store.del_secret(self._vpn_key(self.store.profiles[idx]))
        del self.store.profiles[idx]
        self.store.last_selected = max(0, idx - 1)
        self.store.save()
        self._reload_combo()
        self._load_into_form(self._current_profile())

    def _save_current(self) -> bool:
        p = self._current_profile()
        self._form_into_profile(p)
        if not p.gateway:
            QMessageBox.warning(self, APP_TITLE, "Gateway is required.")
            return False
        if not p.username:
            QMessageBox.warning(self, APP_TITLE, "Username is required.")
            return False
        key = self._vpn_key(p)
        if p.save_vpn_password and self.f_vpn_pw.text():
            self.store.set_secret(key, self.f_vpn_pw.text())
        elif not p.save_vpn_password:
            self.store.del_secret(key)
        self.store.save()
        self._reload_combo()
        self._log_line(f"Profile '{p.name}' saved.")
        return True

    # -- connection ---------------------------------------------------------
    def _ensure_sudo_password(self) -> Optional[str]:
        if self._sudo_pw is not None:
            return self._sudo_pw
        saved = self.store.get_secret("sudo") if self.store.remember_sudo else None
        if saved:
            self._sudo_pw = saved
            return saved
        dlg = SudoDialog(self, remember_default=self.store.remember_sudo)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.password:
            return None
        self._sudo_pw = dlg.password
        self.store.remember_sudo = dlg.remember.isChecked()
        if dlg.remember.isChecked():
            self.store.set_secret("sudo", dlg.password)
        else:
            self.store.del_secret("sudo")
        self.store.save()
        return self._sudo_pw

    def _validate_sudo(self, password: str) -> bool:
        """Cache sudo credentials; returns False if the password is wrong."""
        try:
            res = subprocess.run(
                ["sudo", "-S", "-p", "", "-v"],
                input=password + "\n",
                text=True,
                capture_output=True,
                timeout=15,
            )
            return res.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def _connect(self) -> None:
        if self.state in (self.STATE_CONNECTING, self.STATE_ON, self.STATE_DISCONNECTING):
            return
        if not self._save_current():
            return
        p = self._current_profile()

        vpn_pw = self.f_vpn_pw.text() or (self.store.get_secret(self._vpn_key(p)) or "")
        if not vpn_pw:
            from PyQt6.QtWidgets import QInputDialog
            vpn_pw, ok = QInputDialog.getText(
                self, APP_TITLE, f"VPN password for {p.username}:",
                QLineEdit.EchoMode.Password)
            if not ok or not vpn_pw:
                return

        sudo_pw = self._ensure_sudo_password()
        if sudo_pw is None:
            return

        self._set_state(self.STATE_CONNECTING)
        self._log_line(f"Validating sudo credentials…")
        QApplication.processEvents()
        if not self._validate_sudo(sudo_pw):
            self._sudo_pw = None
            self.store.del_secret("sudo")
            self._log_line("ERROR: sudo authentication failed.")
            self._set_state(self.STATE_OFF)
            QMessageBox.critical(self, APP_TITLE, "sudo authentication failed.")
            return

        args = ["-n", "openconnect",
                f"--protocol={p.protocol}",
                f"--user={p.username}",
                "--passwd-on-stdin"]
        if p.extra_args:
            try:
                args += shlex.split(p.extra_args)
            except ValueError as exc:
                self._log_line(f"ERROR parsing extra args: {exc}")
                self._set_state(self.STATE_OFF)
                return
        args.append(p.server())

        self._active_profile = p
        self.log.clear()
        self._log_line("$ sudo " + " ".join(shlex.quote(a) for a in args[1:]))

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_proc_error)
        self.proc.start("sudo", args)
        if not self.proc.waitForStarted(5000):
            self._log_line("ERROR: failed to start sudo/openconnect.")
            self._set_state(self.STATE_OFF)
            return
        self.proc.write((vpn_pw + "\n").encode())

    def _on_output(self) -> None:
        if not self.proc:
            return
        data = bytes(self.proc.readAllStandardOutput()).decode(errors="replace")
        for line in data.splitlines():
            self._log_line(line)
            if self.state == self.STATE_CONNECTING:
                if any(pat.search(line) for pat in _CONNECTED_PATTERNS):
                    self._set_state(self.STATE_ON)
                elif _CERT_PROMPT.search(line):
                    p = self._active_profile
                    if p and p.auto_accept_cert:
                        self._log_line("[auto-accepting server certificate]")
                        self.proc.write(b"yes\n")
                    else:
                        self._log_line("Untrusted certificate — enable "
                                       "'Auto-accept' or pin with --servercert.")

    def _on_proc_error(self, err) -> None:
        self._log_line(f"Process error: {err}")

    def _on_finished(self, code, status) -> None:
        self._log_line(f"openconnect exited (code {code}).")
        self.proc = None
        if self.state != self.STATE_DISCONNECTING and self.state == self.STATE_CONNECTING:
            self._notify("VPN failed to connect", QSystemTrayIcon.MessageIcon.Warning)
        self._set_state(self.STATE_OFF)

    def _disconnect(self) -> None:
        if self.state in (self.STATE_OFF, self.STATE_DISCONNECTING):
            return
        self._set_state(self.STATE_DISCONNECTING)
        sudo_pw = self._sudo_pw or self._ensure_sudo_password()
        p = self._active_profile or self._current_profile()
        pattern = f"openconnect.*{re.escape(p.gateway)}" if p.gateway else "openconnect"
        self._log_line("Disconnecting…")
        try:
            subprocess.run(
                ["sudo", "-S", "-p", "", "pkill", "-INT", "-f", pattern],
                input=(sudo_pw or "") + "\n",
                text=True, capture_output=True, timeout=15,
            )
        except Exception as exc:
            self._log_line(f"disconnect error: {exc}")
        # Give openconnect a moment to tear down; force-finish if needed.
        QTimer.singleShot(4000, self._force_disconnect)

    def _force_disconnect(self) -> None:
        if self.proc is not None:
            self.proc.kill()
        if self.state != self.STATE_OFF:
            self._set_state(self.STATE_OFF)

    # -- state / display ----------------------------------------------------
    def _set_state(self, state: str) -> None:
        self.state = state
        if state == self.STATE_OFF:
            color, text, icon = "#787878", "Disconnected", self.icon_off
        elif state == self.STATE_CONNECTING:
            color, text, icon = "#e6aa1e", "Connecting…", self.icon_connecting
        elif state == self.STATE_ON:
            color, text, icon = "#28b446", "Connected", self.icon_on
        else:
            color, text, icon = "#e6aa1e", "Disconnecting…", self.icon_connecting

        name = (self._active_profile or self._current_profile()).name
        detail = f"{text} — {name}" if state != self.STATE_OFF else text
        self.status_label.setText(
            f'<b>Status:</b> <span style="color:{color}">●</span> {detail}')
        self.tray.setIcon(icon)
        self.tray.setToolTip(f"{APP_TITLE}: {detail}")
        self.act_status.setText(detail)

        busy = state in (self.STATE_CONNECTING, self.STATE_DISCONNECTING)
        connected = state == self.STATE_ON
        self.btn_connect.setEnabled(state == self.STATE_OFF)
        self.btn_disconnect.setEnabled(connected or state == self.STATE_CONNECTING)
        self.act_connect.setEnabled(state == self.STATE_OFF)
        self.act_disconnect.setEnabled(connected or state == self.STATE_CONNECTING)
        for w in (self.profile_combo, self.btn_new, self.btn_del, self.btn_save):
            w.setEnabled(not busy and not connected)

        if state == self.STATE_ON:
            self._notify(f"Connected to {name}", QSystemTrayIcon.MessageIcon.Information)
        elif state == self.STATE_OFF and getattr(self, "_was_connected", False):
            self._notify("VPN disconnected", QSystemTrayIcon.MessageIcon.Information)
        self._was_connected = connected

    def _log_line(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _notify(self, msg: str, icon=QSystemTrayIcon.MessageIcon.Information) -> None:
        if self.tray.supportsMessages():
            self.tray.showMessage(APP_TITLE, msg, icon, 4000)

    # -- tray / window behaviour -------------------------------------------
    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self._show_window()

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        if self._explicit_quit:
            event.accept()
            return
        # Hide to tray instead of quitting.
        event.ignore()
        self.hide()
        if not self._warned_hide:
            self._warned_hide = True
            self._notify("Still running in the tray. Use the tray icon to quit.")

    def _quit(self) -> None:
        if self.state in (self.STATE_ON, self.STATE_CONNECTING):
            if QMessageBox.question(
                    self, APP_TITLE,
                    "VPN is active. Disconnect and quit?") \
                    != QMessageBox.StandardButton.Yes:
                return
            self._disconnect()
            QTimer.singleShot(1500, self._finish_quit)
            return
        self._finish_quit()

    def _finish_quit(self) -> None:
        self._explicit_quit = True
        self.store.save()
        QApplication.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("warning: no system tray detected; the window will still work.",
              file=sys.stderr)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
