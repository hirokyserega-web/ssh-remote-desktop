"""Application-level SFTP jail and file-command helpers.

File bytes are transferred over the SSH SFTP subsystem (asyncssh provides both
client and server), so we don't reinvent file transfer. The ``files`` control
channel only carries navigation/status commands. This module implements the
**jail**: every path coming from the client is resolved and checked to live
under the session's shared directory, so a client can never reach the whole
server filesystem.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("rd.files")


class JailError(Exception):
    pass


class FileJail:
    """Confines all file operations to ``root`` (the user's shared dir)."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel: str) -> Path:
        """Resolve a client-supplied path against the jail root, safely.

        Rejects absolute escapes and ``..`` traversal that would leave the
        jail. Returns an absolute :class:`Path` guaranteed to be inside root.
        """
        rel = (rel or "").lstrip("/")
        candidate = (self.root / rel).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise JailError(f"path escapes jail: {rel!r}")
        return candidate

    # -- navigation --------------------------------------------------------
    def listdir(self, rel: str = "") -> list[dict]:
        target = self.resolve(rel)
        if not target.is_dir():
            raise JailError(f"not a directory: {rel!r}")
        out = []
        for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                st = entry.stat()
                out.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
            except OSError:
                continue
        return out

    def mkdir(self, rel: str) -> None:
        self.resolve(rel).mkdir(parents=True, exist_ok=True)

    def remove(self, rel: str) -> None:
        target = self.resolve(rel)
        if target == self.root:
            raise JailError("refusing to remove jail root")
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()

    def stat(self, rel: str) -> dict:
        target = self.resolve(rel)
        st = target.stat()
        return {
            "name": target.name,
            "is_dir": target.is_dir(),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        }
