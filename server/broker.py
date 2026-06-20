"""SSH session broker: the long-running daemon that accepts connections.

Built on **asyncssh**. The broker:

* listens for SSH connections and authenticates the user (password via PAM or
  public key from the user's ``authorized_keys``),
* enforces the global concurrent-session limit,
* on the client's mux channel hands off to :class:`ConnectionHandler`,
* serves the SFTP subsystem jailed to each user's shared directory,
* reaps idle / non-persistent sessions.

The transport is a single SSH connection per client; the multiplexed
control/video/input/clipboard/files channels ride inside one SSH session
channel opened by the client, while file *bytes* use the standard SFTP
subsystem on the same connection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

try:
    import asyncssh
except Exception as exc:  # pragma: no cover
    asyncssh = None
    _IMPORT_ERROR = exc

from .auth import authorized_keys_for, check_password
from .connection import ConnectionHandler
from .files import FileJail
from .session import Session, UserInfo

log = logging.getLogger("rd.broker")


class _SSHServer(asyncssh.SSHServer if asyncssh else object):
    """Per-connection asyncssh server: decides auth and remembers the user."""

    def __init__(self, broker: "Broker"):
        self.broker = broker
        self.username = None

    def connection_made(self, conn):
        self._conn = conn

    def connection_lost(self, exc):
        pass

    def begin_auth(self, username):
        self.username = username
        return True  # require some authentication

    # password
    def password_auth_supported(self):
        return self.broker.cfg.allow_password

    def validate_password(self, username, password):
        validator = getattr(self.broker, "password_validator", None)
        if validator is not None:
            if username in validator:
                return validator[username] == password
            return False
        ok = check_password(username, password)
        log.info("password auth for %s: %s", username, "ok" if ok else "denied")
        return ok

    # public key
    def public_key_auth_supported(self):
        return self.broker.cfg.allow_publickey

    def validate_public_key(self, username, key):
        try:
            lines = authorized_keys_for(username)
            for line in lines:
                try:
                    auth_key = asyncssh.import_public_key(line)
                    if key == auth_key:
                        log.info("publickey auth for %s: ok", username)
                        return True
                except Exception:
                    continue
        except Exception as exc:
            log.debug("authorized_keys lookup failed for %s: %s", username, exc)
        return False


class Broker:
    # Test hook: when set, replaces PAM password check. Maps username -> password.
    password_validator: dict[str, str] | None = None

    def __init__(self, cfg):
        if asyncssh is None:  # pragma: no cover
            raise RuntimeError(f"asyncssh is required: {_IMPORT_ERROR}")
        self.cfg = cfg
        self._sessions: dict[str, Session] = {}
        self._persistent: dict[tuple[str, str], Session] = {}
        self._lock = threading.Lock()
        self._server = None
        self._reaper_task: asyncio.Task | None = None

    # -- session pool ------------------------------------------------------
    def acquire_session(self, user: UserInfo, *, geometry, persistent) -> Session:
        with self._lock:
            if persistent:
                key = (user.name, f"{geometry[0]}x{geometry[1]}")
                existing = self._persistent.get(key)
                if existing is not None and not existing._stopped:
                    existing.touch()
                    return existing
            if len(self._sessions) >= self.cfg.max_sessions:
                raise RuntimeError("session limit reached")
            from .backend import detect_backend_kind
            kind = detect_backend_kind(forced=self.cfg.backend)
            session = Session(self.cfg, user, backend_kind=kind,
                              geometry=geometry, persistent=persistent)
        session.start()  # outside the lock (spawns processes)
        with self._lock:
            self._sessions[session.session_id] = session
            if persistent:
                self._persistent[(user.name, f"{geometry[0]}x{geometry[1]}")] = session
        return session

    def release_session(self, session: Session):
        if session.persistent:
            session.touch()
            log.info("session %s kept (persistent)", session.session_id)
            return
        self._drop(session)

    def _drop(self, session: Session):
        with self._lock:
            self._sessions.pop(session.session_id, None)
            for k, v in list(self._persistent.items()):
                if v is session:
                    self._persistent.pop(k, None)
        session.stop()

    def jail_for(self, user: UserInfo) -> FileJail:
        shared = self.cfg.shared_dir
        if shared.startswith("~"):
            shared = os.path.join(user.home, shared.lstrip("~/"))
        elif not os.path.isabs(shared):
            shared = os.path.join(user.home, shared)
        return FileJail(shared)

    # -- asyncssh wiring ---------------------------------------------------
    async def _handle_session(self, process):
        """Handle the client's interactive channel (our multiplexed stream)."""
        username = process.get_extra_info("username") or process.channel.get_extra_info("username")
        # asyncssh exposes the authenticated username on the connection.
        conn = process.get_extra_info("connection")
        username = conn.get_extra_info("username") if conn else username
        handler = ConnectionHandler(self.cfg, self, username, process.stdin, process.stdout)
        try:
            await handler.run()
        except Exception:
            log.exception("connection handler crashed")
        finally:
            process.exit(0)

    def _make_sftp_factory(self):
        broker = self

        class JailedSFTP(asyncssh.SFTPServer):
            def __init__(self, chan):
                conn = chan.get_extra_info("connection")
                username = conn.get_extra_info("username")
                user = UserInfo(username)
                jail = broker.jail_for(user)
                super().__init__(chan, chroot=str(jail.root))

        return JailedSFTP

    async def start(self):
        host_key_path = os.path.expanduser(self.cfg.host_key)
        self._ensure_host_key(host_key_path)

        def server_factory():
            return _SSHServer(self)

        self._server = await asyncssh.create_server(
            server_factory,
            self.cfg.host, self.cfg.port,
            server_host_keys=[host_key_path],
            process_factory=self._handle_session,
            sftp_factory=self._make_sftp_factory() if self.cfg.files_enabled else None,
            allow_scp=False,
            encoding=None,
        )
        self._reaper_task = asyncio.create_task(self._reaper())
        log.info("broker listening on %s:%d", self.cfg.host, self.cfg.port)

    def _ensure_host_key(self, path: str):
        if os.path.exists(path):
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(path)
        log.info("generated SSH host key at %s", path)

    async def _reaper(self):
        timeout = self.cfg.idle_timeout
        while True:
            await asyncio.sleep(15)
            if timeout <= 0:
                continue
            for session in list(self._sessions.values()):
                if session.idle_seconds() > timeout:
                    log.info("reaping idle session %s", session.session_id)
                    self._drop(session)

    async def serve_forever(self):
        await self.start()
        await self.serve_forever_after_start()

    async def serve_forever_after_start(self):
        """Run the broker until cancelled, then shut it down.

        Counterpart to :meth:`start`: assumes the SSH listener is already up
        (so this can be scheduled as a task after ``await broker.start()``).
        Used by ``rd-server`` so the pidfile is written between ``start()``
        and the run-forever wait.
        """
        try:
            await asyncio.Future()  # run until cancelled
        finally:
            await self.shutdown()

    async def shutdown(self):
        if self._reaper_task:
            self._reaper_task.cancel()
        for session in list(self._sessions.values()):
            session.stop()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
