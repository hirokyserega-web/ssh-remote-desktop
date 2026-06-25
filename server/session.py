"""Per-connection graphical session management (X11 Xvfb / headless Wayland).

On each connection the broker creates a :class:`Session` for the authenticated
user. A session:

* allocates a free display number / wayland socket name,
* launches an isolated virtual display server **as that user** (Xvfb for X11,
  ``sway --headless`` / ``weston`` / ``kwin_wayland`` / ``gnome-remote-desktop``
  for Wayland), with its own ``XAUTHORITY`` cookie (X11),
* optionally starts the user's window manager / DE,
* exposes the chosen :class:`DisplayBackend`,
* tears everything down on disconnect (unless persistent), with an idle
  timeout and a global concurrent-session cap enforced by the broker.

Privilege handling: the broker typically runs as root (needed for PAM auth,
user switching and starting X servers). Each session drops to the target
user's UID/GID via a ``preexec_fn`` so the virtual display, WM and apps all run
as that user with their own ``$HOME`` / environment.
"""

from __future__ import annotations

import logging
import os
try:
    import pwd
except ImportError:  # Windows: server is Linux-only; guard for test collection.
    pwd = None
import secrets
import shutil
import signal
import socket
import subprocess
import time

from .backend import create_backend

log = logging.getLogger("rd.session")


# ---------------------------------------------------------------------------
# User / privilege helpers
# ---------------------------------------------------------------------------
# Re-exported by :mod:`server.auth` as :data:`SERVER_IS_LINUX_ONLY`; kept
# locally so :class:`UserInfo` can fail-fast without a cross-module import
# at construction time. Same RuntimeError shape as in auth.py.
_SERVER_IS_LINUX_ONLY = RuntimeError("server-side user lookup requires POSIX pwd (Linux-only)")


class UserInfo:
    def __init__(self, name: str):
        if pwd is None:
            # Non-POSIX host (Windows / headless test runner): the session
            # machinery is Linux-only (needs real UIDs, X display sockets,
            # process demotion). Refuse explicitly instead of crashing on
            # ``None.getpwnam``.
            raise _SERVER_IS_LINUX_ONLY
        rec = pwd.getpwnam(name)
        self.name = rec.pw_name
        self.uid = rec.pw_uid
        self.gid = rec.pw_gid
        self.home = rec.pw_dir
        self.shell = rec.pw_shell

    def base_env(self, extra: dict | None = None) -> dict:
        env = {
            "HOME": self.home,
            "USER": self.name,
            "LOGNAME": self.name,
            "SHELL": self.shell or "/bin/sh",
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin",
            "XDG_RUNTIME_DIR": f"/run/user/{self.uid}",
        }
        if extra:
            env.update(extra)
        return env


def _demote(uid: int, gid: int):
    """Return a preexec_fn that switches the child process to uid/gid.

    Only effective when the parent is privileged (root). When not root, the
    setgid/setuid calls raise and we skip them so unprivileged dev/testing of a
    single-user session still works.
    """
    def preexec():  # pragma: no cover - runs in the child
        try:
            os.setgid(gid)
            if pwd is not None:
                os.initgroups(pwd.getpwuid(uid).pw_name, gid)
            os.setuid(uid)
        except PermissionError:
            pass
        os.setsid()
    return preexec


def _free_display_number(start: int = 10, end: int = 200) -> int:
    """Find a free X display number by checking the lock file + socket."""
    for n in range(start, end):
        lock = f"/tmp/.X{n}-lock"
        sock = f"/tmp/.X11-unix/X{n}"
        if not os.path.exists(lock) and not os.path.exists(sock):
            return n
    raise RuntimeError("no free X display number")


def _free_display_number_candidates(start: int = 10, end: int = 200):
    """Yield candidate X display numbers whose lock + socket are both free.

    Re-checked on each iteration so two concurrent sessions don't both pick the
    same number (Xvfb's own ``/tmp/.X{n}-lock`` is the final atomic arbiter; we
    just skip obviously-taken numbers before spawning, then verify the socket
    actually appeared — see :meth:`Session._start_x11`).
    """
    for n in range(start, end):
        lock = f"/tmp/.X{n}-lock"
        sock = f"/tmp/.X11-unix/X{n}"
        if not os.path.exists(lock) and not os.path.exists(sock):
            yield n


def _free_tcp_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _terminate(proc, *, timeout: float = 2.0) -> None:
    """SIGTERM a child, wait briefly, then SIGKILL if it ignores the term."""
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class DisplayServerError(RuntimeError):
    """A display server (Xvfb / Wayland compositor) could not be started.

    Raised with an actionable message — the missing binary + the package to
    install, or a readiness-timeout cause — so ``rd-server --foreground``
    prints a clear diagnosis instead of the opaque downstream exception that
    used to surface as "неизвестная ошибка" when the backend connected to a
    display server that never came up.
    """


def _wait_for_file(path: str, proc, *, timeout: float = 5.0,
                   poll: float = 0.1) -> bool:
    """Poll for ``path`` to appear while ``proc`` stays alive.

    Used to confirm an Xvfb/Wayland socket actually exists before the backend
    connects — replacing the old fixed ``time.sleep(1.0)`` that connected to a
    display server which might never have started. Returns False if the process
    exits or the timeout elapses without the file appearing.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # display server died
        if os.path.exists(path):
            return True
        time.sleep(poll)
    return False


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, cfg, user: UserInfo, *, backend_kind: str,
                 geometry: tuple[int, int], persistent: bool):
        self.cfg = cfg
        self.user = user
        self.backend_kind = backend_kind
        self.geometry = geometry
        self.persistent = persistent
        self.session_id = secrets.token_hex(4)

        self.display: str | None = None
        self.wayland_display: str | None = None
        self._procs: list[subprocess.Popen] = []
        self._xauth = None
        self.backend = None
        self._last_activity = time.time()
        self._stopped = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.backend_kind == "wayland":
            env = self._start_wayland()
        else:
            env = self._start_x11()
        self.backend = create_backend(
            self.backend_kind, env, self.geometry, cursor_mode=self.cfg.cursor_mode
        )
        # _
        self.backend.start()
        self._maybe_start_wm(env)
        log.info("session %s up: backend=%s display=%s wayland=%s user=%s",
                 self.session_id, self.backend_kind, self.display,
                 self.wayland_display, self.user.name)

    def _start_x11(self) -> dict:
        w, h = self.geometry
        xvfb = shutil.which(self.cfg.xvfb_bin)
        if not xvfb:
            raise DisplayServerError(
                f"Xvfb binary '{self.cfg.xvfb_bin}' not found. The x11 backend "
                f"needs Xvfb to provide a headless display. Install it: "
                f"Debian/Ubuntu: sudo apt install xvfb; Arch: sudo pacman -S "
                f"xorg-server-xvfb; Fedora: sudo dnf install xorg-x11-server-Xvfb."
            )
        # Per-session XAUTHORITY cookie (shared across display-number retries).
        runtime = f"/run/user/{self.user.uid}"
        xauth_dir = runtime if os.path.isdir(runtime) else f"/tmp/rd-{self.user.name}"
        os.makedirs(xauth_dir, exist_ok=True)
        try:
            os.chown(xauth_dir, self.user.uid, self.user.gid)
        except PermissionError:
            pass
        self._xauth = os.path.join(xauth_dir, f"Xauthority-{self.session_id}")
        cookie = secrets.token_hex(16)

        # Try candidate display numbers until one actually comes up. Xvfb's own
        # /tmp/.X{n}-lock is the atomic arbiter; if another session raced us for
        # a number, Xvfb exits and the socket never appears — we detect that and
        # retry the next number instead of connecting to a dead display.
        last_err = "no free X display number"
        for n in _free_display_number_candidates():
            self.display = f":{n}"
            env = self.user.base_env({"DISPLAY": self.display, "XAUTHORITY": self._xauth})
            try:
                subprocess.run(
                    ["xauth", "-f", self._xauth, "add", self.display, ".", cookie],
                    env=env, check=False, capture_output=True,
                    preexec_fn=_demote(self.user.uid, self.user.gid),
                )
            except FileNotFoundError:
                log.warning("xauth not found; display cookie not set")
            cmd = [xvfb, self.display, "-screen", "0",
                   f"{w}x{h}x{self.cfg.session_depth}",
                   "-auth", self._xauth, "-nolisten", "tcp"]
            proc = self._spawn(cmd, env)
            if proc is None:
                raise DisplayServerError(f"Xvfb failed to spawn: {cmd[0]}")
            socket_path = f"/tmp/.X11-unix/X{n}"
            if _wait_for_file(socket_path, proc, timeout=5.0):
                return env
            last_err = (f"Xvfb on {self.display} did not become ready "
                        f"(alive={proc.poll() is None})")
            log.warning("%s; trying another display number", last_err)
            _terminate(proc)
            self.display = None
        raise DisplayServerError(
            f"could not start Xvfb on any free display number: {last_err}. "
            f"Check that Xvfb is installed and /tmp/.X11-unix is writable."
        )

    def _start_wayland(self) -> dict:
        idx = _free_display_number(1, 100)
        self.wayland_display = f"wayland-{idx}"
        runtime = f"/run/user/{self.user.uid}"
        # Mirror the x11 path's defensive fallback: /run/user/<uid> may be
        # unwritable when the server isn't root (CI, restricted /run), so fall
        # back to a per-user temp dir so the compositor still gets a place for
        # its socket instead of crashing on makedirs before anything starts.
        try:
            os.makedirs(runtime, exist_ok=True)
        except (PermissionError, OSError):
            runtime = f"/tmp/rd-runtime-{self.user.uid}"
            os.makedirs(runtime, exist_ok=True)
        try:
            os.chown(runtime, self.user.uid, self.user.gid)
            os.chmod(runtime, 0o700)
        except PermissionError:
            pass
        env = self.user.base_env({
            "WAYLAND_DISPLAY": self.wayland_display,
            "XDG_RUNTIME_DIR": runtime,
            "WLR_BACKENDS": "headless",
            "WLR_LIBINPUT_NO_DEVICES": "1",
            "XDG_SESSION_TYPE": "wayland",
        })
        comp = self.cfg.wayland_compositor
        if comp == "weston":
            cmd = ["weston", "--backend=headless-backend.so",
                   f"--socket={self.wayland_display}",
                   f"--width={self.geometry[0]}", f"--height={self.geometry[1]}"]
        elif comp == "kwin":
            cmd = ["kwin_wayland", "--virtual",
                   f"--width={self.geometry[0]}", f"--height={self.geometry[1]}"]
        elif comp == "gnome":
            cmd = ["gnome-remote-desktop-daemon", "--headless"]
        else:  # sway (default)
            cmd = ["sway"]
        # Refuse to start with a clear, actionable error if the compositor
        # binary is missing — instead of swallowing it in _spawn and failing
        # later when the backend connects to a socket that was never created.
        binary = shutil.which(cmd[0])
        if not binary:
            raise DisplayServerError(
                f"Wayland compositor '{cmd[0]}' not found (wayland_compositor="
                f"{comp!r}). The wayland backend needs a headless compositor. "
                f"Install one: Debian/Ubuntu: sudo apt install sway; Arch: "
                f"sudo pacman -S sway; Fedora: sudo dnf install sway. "
                f"Or use backend = \"x11\" (Xvfb) for a fully working session."
            )
        cmd[0] = binary
        proc = self._spawn(cmd, env)
        if proc is None:
            raise DisplayServerError(f"compositor failed to spawn: {cmd[0]}")
        # Wait for the Wayland socket to appear in XDG_RUNTIME_DIR before
        # letting the backend connect — replaces the old fixed sleep(1.0).
        socket_path = os.path.join(runtime, self.wayland_display)
        if not _wait_for_file(socket_path, proc, timeout=6.0):
            raise DisplayServerError(
                f"Wayland compositor {cmd[0]} did not create its socket "
                f"{socket_path} within 6s (alive={proc.poll() is None}). "
                f"The wayland backend is experimental; for a working session "
                f"use backend = \"x11\" with Xvfb installed."
            )
        return env

    def _maybe_start_wm(self, env: dict):
        wm = (self.cfg.window_manager or "").strip()
        if wm and self.backend_kind == "x11":
            self._spawn(wm.split(), env)

    def _spawn(self, cmd: list[str], env: dict):
        log.debug("spawn (%s): %s", self.user.name, " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=_demote(self.user.uid, self.user.gid),
                cwd=self.user.home if os.path.isdir(self.user.home) else "/tmp",
            )
            self._procs.append(proc)
            return proc
        except FileNotFoundError as exc:
            log.warning("cannot start %s: %s", cmd[0], exc)

    # -- activity / idle ---------------------------------------------------
    def touch(self):
        self._last_activity = time.time()

    def idle_seconds(self) -> float:
        return time.time() - self._last_activity

    # -- teardown ----------------------------------------------------------
    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            if self.backend is not None:
                self.backend.stop()
        except Exception:
            pass
        for proc in reversed(self._procs):
            try:
                proc.send_signal(signal.SIGTERM)
            except Exception:
                pass
        deadline = time.time() + 3
        for proc in reversed(self._procs):
            try:
                timeout = max(0.1, deadline - time.time())
                proc.wait(timeout=timeout)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._xauth and os.path.exists(self._xauth):
            try:
                os.remove(self._xauth)
            except OSError:
                pass
        log.info("session %s stopped", self.session_id)
