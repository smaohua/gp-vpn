# OpenConnect VPN GUI

A small PyQt6 tray application that wraps `openconnect`, so you can store
connection profiles, connect/disconnect with a click, and keep an icon in the
system tray that reflects the connection state.

It reproduces commands like:

```
sudo openconnect --protocol=gp --user=phs02 pa-gzsite.jxsdwan.com:8443
```

## Features

- Multiple saved profiles (protocol, gateway, port, username, extra args).
- Connect / disconnect from the window **or** the tray icon menu.
- Tray icon colour shows the state: grey = off, amber = connecting, green = on.
- Elevates with `sudo -S`; your sudo password is asked **in the app** and can be
  remembered for the session. No `sudoers` changes required.
- Passwords stored in `~/.config/openconnect-gui/secrets.json` with `0600`
  permissions (or your system keyring if `python-keyring` is installed).
- Live openconnect log shown in the window.

## Requirements

Arch Linux packages:

```
sudo pacman -S --needed openconnect python-pyqt6
```

Optional, for encrypted secret storage instead of a `0600` file:

```
sudo pacman -S --needed python-keyring gnome-keyring   # or kwallet
```

## Install

```
./install.sh
```

This installs a launcher at `~/.local/bin/openconnect-gui` and a desktop entry
("OpenConnect VPN") into your application menu. Nothing is installed as root.

To run without installing:

```
python3 openconnect_gui.py
```

## Usage

1. Fill in the connection settings. For your example:
   - **Protocol:** `gp`
   - **Gateway:** `pa-gzsite.jxsdwan.com`
   - **Port:** `8443`
   - **Username:** `phs02`
2. Optionally tick **Save VPN password**, then **Save profile**.
3. Click **Connect**. You'll be asked for your sudo password the first time
   (tick "Remember for this session" to avoid retyping).
4. Close the window to keep it running in the tray. Right-click the tray icon to
   connect, disconnect, show the window, or quit.

### Notes

- **Server certificate:** if openconnect refuses the server certificate (it
  appears in the log), either add `--servercert pin-sha256:...` to **Extra args**
  or tick **Auto-accept server certificate (insecure)**.
- **Extra args** accepts any openconnect flags, e.g. `--os=linux`,
  `--csd-wrapper=...`, `--servercert ...`.
- **Disconnect** sends `SIGINT` to openconnect via `sudo pkill`, so routes and
  DNS are torn down cleanly.

## Files

- `~/.config/openconnect-gui/config.json` — profiles (no passwords).
- `~/.config/openconnect-gui/secrets.json` — passwords, `0600` (only if keyring
  is unavailable).

## Security

The sudo password and VPN password are kept in memory while the app runs. If you
choose to save them, they live in a `0600` file readable only by your user
(unencrypted) unless `python-keyring` with a working secret service is present,
in which case the keyring is used. Untick the save boxes to be prompted each
time instead.
