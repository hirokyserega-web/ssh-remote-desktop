"""Trust-on-first-use host-key verification for asyncssh connections.

The SSH transport has to decide whether to trust a server it has never seen
before (or whose key has changed). OpenSSH does this with the
``~/.ssh/known_hosts`` file: on first connect the user is shown the key
fingerprint and asked to confirm; the key is then recorded and any future
mismatch is treated as a potential MITM.

This module provides the same behaviour for the GUI client:

* :class:`KnownHostsStore` -- a tiny append-only store of ``host:port ->
  base64(ssh-public-key-blob)`` kept in the user's ``~/.ssh`` (or a configurable
  path). It is independent of OpenSSH's own ``known_hosts`` so we never mutate
  the user's real file; asyncssh is pointed at our file instead.
* :class:`TofuClient` -- an :class:`asyncssh.SSHClient` subclass whose
  ``validate_host_public_key`` callback implements the TOFU policy:

      - known + matches      -> accept silently
      - known + mismatch     -> reject (raise :class:`HostKeyMismatch`)
      - unknown              -> ask the user via a thread-safe callback. On
        acceptance the key is appended to the store; on rejection a
        :class:`HostKeyRejected` is raised.

The user-confirmation callback runs on the asyncio transport thread, so the GUI
must marshal the question onto the Qt thread (see
:func:`ask_host_key_dialog` which returns a bool from a blocking Qt dialog).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import asyncssh

log = logging.getLogger("rd.client.hostkeys")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class HostKeyError(Exception):
    """Base class for host-key verification failures."""


class HostKeyMismatch(HostKeyError):
    """The server's key differs from the one recorded on first connect."""


class HostKeyRejected(HostKeyError):
    """The user declined to trust an unknown host key."""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HostKeyEntry:
    host: str
    port: int
    key_blob_b64: str      # base64 of the raw SSH public key blob
    fingerprint: str       # "SHA256:..." OpenSSH-style


class KnownHostsStore:
    """A simple append-only ``host:port -> key`` store.

    The file format is one entry per line::

        <host> <port> <base64-key-blob> <SHA256:fingerprint>

    It is intentionally NOT OpenSSH's ``known_hosts`` format -- we keep our own
    file (``rd_known_hosts``) so we never touch the user's real SSH state. The
    asyncssh ``known_hosts`` argument is set to ``None`` (accept nothing
    blindly) and we do the validation ourselves via :class:`TofuClient`.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None):
        if path is None:
            ssh_dir = Path.home() / ".ssh"
            path = ssh_dir / "rd_known_hosts"
        self.path = Path(path)

    # -- IO ----------------------------------------------------------------
    def _load(self) -> dict[tuple[str, int], HostKeyEntry]:
        out: dict[tuple[str, int], HostKeyEntry] = {}
        if not self.path.exists():
            return out
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                host, port_s, blob, fp = parts[0], parts[1], parts[2], parts[3]
                try:
                    port = int(port_s)
                except ValueError:
                    continue
                out[(host, port)] = HostKeyEntry(host, port, blob, fp)
        except OSError as exc:
            log.warning("cannot read known-hosts %s: %s", self.path, exc)
        return out

    def _append(self, entry: HostKeyEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600: the file lists hosts we trust, treat as sensitive.
        try:
            if not self.path.exists():
                self.path.touch(0o600)
                os.chmod(self.path, 0o600)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(f"{entry.host} {entry.port} {entry.key_blob_b64} {entry.fingerprint}\n")
        except OSError as exc:
            log.warning("cannot write known-hosts %s: %s", self.path, exc)

    # -- public API --------------------------------------------------------
    def lookup(self, host: str, port: int) -> HostKeyEntry | None:
        return self._load().get((host, int(port)))

    def add(self, host: str, port: int, key) -> HostKeyEntry:
        """Record ``key`` (an asyncssh SSHKey) for ``host:port``.

        If an entry already exists it is NOT overwritten -- a mismatch should
        have been caught earlier by the caller. Returns the stored entry.
        """
        blob_b64 = _key_blob_b64(key)
        entry = HostKeyEntry(host=host, port=int(port),
                             key_blob_b64=blob_b64,
                             fingerprint=key.get_fingerprint())
        self._append(entry)
        return entry

    def replace(self, host: str, port: int, key) -> HostKeyEntry:
        """Overwrite the entry for ``host:port`` with ``key``.

        Used after the user explicitly accepts a changed key. Rewrites the
        whole file with the offending line replaced.
        """
        blob_b64 = _key_blob_b64(key)
        entry = HostKeyEntry(host=host, port=int(port),
                             key_blob_b64=blob_b64,
                             fingerprint=key.get_fingerprint())
        entries = self._load()
        entries[(host, int(port))] = entry
        self._rewrite(entries)
        return entry

    def _rewrite(self, entries: dict[tuple[str, int], HostKeyEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in entries.values():
                fh.write(f"{e.host} {e.port} {e.key_blob_b64} {e.fingerprint}\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


def _key_blob_b64(key) -> str:
    """Return the base64-encoded raw SSH public key blob (no comment/wrapper)."""
    import base64
    return base64.b64encode(key.public_data).decode("ascii")


# ---------------------------------------------------------------------------
# SSHClient implementing TOFU
# ---------------------------------------------------------------------------
# A confirmation callback receives the host, port, fingerprint and a bool
# indicating whether this is a *first-time* ask (unknown host) or a *changed*
# key (potential MITM). It returns True to accept, False to reject. For a
# changed key the GUI should present a scarier dialog and ideally require an
# explicit "I understand the risk" checkbox.
AskFn = "Callable[[str, int, str, bool], Awaitable[bool]]"
ConfirmFn = AskFn


class TofuClient(asyncssh.SSHClient):
    """asyncssh client that does TOFU host-key validation.

    Pass a :class:`KnownHostsStore` and an ``ask`` coroutine to the
    constructor, then pass the instance to ``asyncssh.connect(..., client=...)``
    with ``known_hosts=None`` (so asyncssh defers all validation to us).

    ``ask(host, port, fingerprint, first_time)`` is awaited on the asyncio
    transport loop; it must return ``True`` to accept the key and ``False`` to
    reject it. The GUI implementation marshals the question onto the Qt thread
    via a signal and resolves an :class:`asyncio.Event` when the user answers
    (see :class:`client.transport.Transport`).
    """

    def __init__(self, store: "KnownHostsStore", ask: "Callable[[str, int, str, bool], Awaitable[bool]]"):
        self._store = store
        self._ask = ask
        # Resolved at connection_made time so the callback has them.
        self._host = ""
        self._port = 22

    def connection_made(self, conn: "asyncssh.SSHClientConnection") -> None:  # type: ignore[override]
        # asyncssh fills conn.get_extra_info('peername') once the transport is
        # up; grab host/port here as a fallback for the validation callback.
        info = conn.get_extra_info("peername") or ("", 0)
        self._host, self._port = (info[0], info[1]) if info else ("", 22)

    async def validate_host_public_key(self, host: str, addr: str, port: int, key) -> bool:
        """Return True iff the host key is trusted under TOFU.

        asyncssh awaits this coroutine when ``known_hosts`` is None (or the host
        is not in the file). Returning False aborts the handshake; raising an
        exception propagates to ``asyncssh.connect`` as the connection error.
        """
        host = host or self._host
        port = int(port or self._port or 22)
        fp = key.get_fingerprint()
        known = self._store.lookup(host, port)

        if known is None:
            # First contact: ask the user.
            try:
                accepted = bool(await self._ask(host, port, fp, True))
            except Exception as exc:
                log.warning("host-key ask callback raised: %s", exc)
                accepted = False
            if not accepted:
                raise HostKeyRejected(f"unknown host {host}:{port} key rejected by user")
            self._store.add(host, port, key)
            log.info("TOFU: recorded host key for %s:%d (%s)", host, port, fp)
            return True

        # Known host: compare the recorded blob to the one presented.
        presented_b64 = _key_blob_b64(key)
        if known.key_blob_b64 == presented_b64:
            return True

        # Mismatch: the dangerous case. Ask again with first_time=False so the
        # GUI can show a sterner warning. If the user accepts, overwrite the
        # stored entry (they have explicitly chosen to trust the new key).
        try:
            accepted = bool(await self._ask(host, port, fp, False))
        except Exception as exc:
            log.warning("host-key ask callback raised: %s", exc)
            accepted = False
        if not accepted:
            raise HostKeyMismatch(
                f"host key for {host}:{port} changed (expected {known.fingerprint}, "
                f"got {fp})"
            )
        self._store.replace(host, port, key)
        log.warning("TOFU: user accepted CHANGED host key for %s:%d (%s)", host, port, fp)
        return True
