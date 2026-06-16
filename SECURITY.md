# Security Policy

## Threat model

The server accepts SSH connections from untrusted networks. It runs
multitenant — multiple users share the same host. Therefore the design
prioritises:

1. **Authentication of the remote user** before any code runs.
2. **Privilege separation** between the broker (root) and per-session
   workers (target user).
3. **Filesystem isolation** for the file-share / SFTP subsystem.
4. **Resource isolation** between concurrent users.

## What we already do

- **Authentication.** Both public-key (compared against
  `~user/.ssh/authorized_keys` line-by-line, same as OpenSSH) and
  password (PAM, service `login`). Either can be disabled in
  `server.toml`.
- **Privilege drop.** On every connection, after a successful auth, the
  worker drops from root to the target UID/GID via `setgid` +
  `initgroups` + `setuid` (`preexec_fn` in
  `file 'server/session.py'`). All subprocesses (Xvfb, sway, WM) inherit
  the demoted identity. The broker itself never re-escalates.
- **XAUTHORITY cookie** is generated per session via `xauth add` with
  128 bits of randomness. The cookie file lives in
  `/run/user/<uid>` (or `/tmp/rd-<user>` if the runtime dir is not
  writable) and is removed on session teardown.
- **Wayland session dirs.** `XDG_RUNTIME_DIR` is created per user
  (`/run/user/<uid>`) with mode `0700` and `chown` to the user.
- **SFTP jail.** All incoming paths are resolved against the session's
  `shared_dir` and rejected if they would escape (`FileJail.resolve` in
  `file 'server/files.py'`). Symlink resolution and `..` traversal are
  blocked.
- **Clipboard cap.** Hard size limit per message
  (`clipboard_max_bytes`, default 1 MiB) and a per-tunnel toggle on the
  client side.
- **Resource limits.** `max_sessions` (concurrent sessions),
  `idle_timeout` (kills idle non-persistent sessions). The server runs
  an asyncio reaper task.
- **Host key.** The server's SSH host key is generated on first start
  and persisted under `~/.config/ssh-remote-desktop/`. Reusing a
  long-lived host key is recommended for production.

## What is **not** yet covered

- **No reverse proxy / WAF** is shipped. If you expose the broker to
  the internet, put it behind a firewall or an SSH bastion.
- **No rate limiting** beyond asyncssh defaults. Consider fail2ban
  matching on the auth log if exposing publicly.
- **No sandboxing** of the user's window manager. Anything the target
  user can run inside their X/Wayland session, they can run. The
  capture/input code itself is only the bridge.
- **Clipboard image/file-list formats** are spec-listed as optional and
  **not implemented** in this revision. Treat the clipboard channel as
  text-only.
- **`python-pam` fallback** — if `python-pam` is not installed, the
  broker refuses password auth (fail-closed). The broker does **not**
  fall back to a weaker password check.

## Reporting vulnerabilities

Please **do not** open public issues for suspected security bugs.
Email: `hirokyserega-web@users.noreply.github.com` (use a
throwaway/themed mailbox; rotate keys after a disclosure).

Include: a reproducer, expected vs actual behaviour, impact. We will
acknowledge within 72 hours and aim to ship a fix or mitigation within
14 days for critical issues.
