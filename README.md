# SSH Remote Desktop

Python client/server remote desktop over **SSH only**.

## What it does

- Remote graphical desktop over a single SSH connection.
- Separate logical channels for control, video, input, clipboard, and files.
- Client GUI on **Windows** and **Linux** (Qt / PySide6).
- Server on **Linux**, with automatic **X11** or **Wayland** backend selection.
- Built-in SSH key generation in the client.
- Clipboard sync and SFTP-based file sharing.

## Project layout

- `common/` — protocol, frame multiplexer, config
- `crypto/` — SSH key generation / storage
- `server/` — SSH broker, sessions, X11 / Wayland backends, encoders
- `client/` — Qt GUI, transport, decoder, dialogs
- `requirements.txt` — Python dependencies
- `build_*.spec` — Nuitka/PyInstaller build inputs

## Quick start (development)

```bash
pip install -r requirements.txt
```

Run server:

```bash
python -m server --host 0.0.0.0 --port 2222
```

Run client:

```bash
python -m client --host 127.0.0.1 --port 2222 --user yourname
```

## SSH setup

- Password auth uses PAM (if available on the server).
- Public-key auth reads `~/.ssh/authorized_keys` for the target user.
- The client can generate keys from **SSH keys** in the toolbar.

## Server notes

### X11 backend

- Uses `Xvfb` for isolated sessions.
- Capture/input paths prefer `python-xlib` + `mss` + `XTEST`.
- Clipboard uses `xclip` when present.

### Wayland backend

- Uses a headless compositor (`sway` by default).
- Capture uses PipeWire / portal when available; otherwise a placeholder frame keeps the pipeline alive.
- Input uses `uinput` when available, with `ydotool` as fallback.
- Clipboard uses `wl-copy` / `wl-paste`.

## Client notes

- On Linux, Qt platform is chosen automatically.
- `wayland;xcb` is used when Wayland is present, so XWayland is the fallback.
- Wayland cannot globally grab system shortcuts, so the toolbar exposes special combos.

## Clipboard

- Text sync is enabled by default.
- Loop protection uses the `origin` field.
- Size is limited by `clipboard_max_bytes`.

## Files / SFTP

- File bytes are transferred over SFTP on the same SSH connection.
- The `files` control channel is used for list/stat/mkdir/remove/status only.
- Server access is jailed to the configured shared folder.

## Build

Preferred: **Nuitka**.

### Client (Windows)

- Use `build_client_windows.spec`.
- Bundle Qt plugins and all Python dependencies.

### Client (Linux)

- Use `build_client_linux.spec`.

### Server (Linux)

- Use `build_server_linux.spec`.

If you prefer PyInstaller, the source layout is compatible with `--onefile` builds, but Nuitka is the recommended path.

## Config

Both client and server accept TOML or JSON config files.

- Server: `server.toml`
- Client: `client.toml`

CLI flags override config files.
