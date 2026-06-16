# ssh-remote-desktop

SSH-only remote desktop (X11 + Wayland) written in Python.

- `file 'common'` — wire protocol, framing, multiplexer, shared config loader
- `file 'crypto'` — in-app SSH key generation (Ed25519 / RSA)