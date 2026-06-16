"""Client-side SFTP file transfer over the shared SSH connection.

Uses asyncssh's SFTP client on the same connection the video/input ride on.
Transfers run on the transport's asyncio loop; progress is reported via a
callback so the GUI can show a progress bar. Large files are streamed in chunks
by asyncssh itself; we wrap put/get with a progress handler and support
cancellation through an :class:`asyncio.Event`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

log = logging.getLogger("rd.client.files")


class SFTPTransfer:
    def __init__(self, transport):
        self._t = transport

    async def _sftp(self):
        conn = self._t.get_connection()
        if conn is None:
            raise RuntimeError("not connected")
        return await conn.start_sftp_client()

    async def listdir(self, remote_path: str = "."):
        async with await self._sftp() as sftp:
            names = await sftp.readdir(remote_path or ".")
            out = []
            for entry in names:
                if entry.filename in (".", ".."):
                    continue
                attrs = entry.attrs
                out.append({
                    "name": entry.filename,
                    "is_dir": bool((attrs.permissions or 0) & 0o040000),
                    "size": attrs.size or 0,
                    "mtime": attrs.mtime or 0,
                })
            return out

    async def upload(self, local_path: str, remote_path: str,
                     progress: Callable[[int, int], None] | None = None,
                     cancel=None):
        async with await self._sftp() as sftp:
            def _prog(src, dst, transferred, total):
                if progress:
                    progress(transferred, total)
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError()
            await sftp.put(local_path, remote_path, progress_handler=_prog,
                           block_size=256 * 1024)

    async def download(self, remote_path: str, local_path: str,
                       progress: Callable[[int, int], None] | None = None,
                       cancel=None):
        async with await self._sftp() as sftp:
            def _prog(src, dst, transferred, total):
                if progress:
                    progress(transferred, total)
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError()
            await sftp.get(remote_path, local_path, progress_handler=_prog,
                           block_size=256 * 1024)

    async def mkdir(self, remote_path: str):
        async with await self._sftp() as sftp:
            await sftp.makedirs(remote_path, exist_ok=True)

    async def remove(self, remote_path: str):
        async with await self._sftp() as sftp:
            try:
                await sftp.remove(remote_path)
            except Exception:
                await sftp.rmdir(remote_path)


# Backwards-compatible alias used by the GUI.
FileTransfer = SFTPTransfer
