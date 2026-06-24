"""Authentication helpers: PAM password check and authorized_keys lookup.

The SSH server (asyncssh) drives the protocol; these helpers decide whether a
given user's password or public key is acceptable.

* :func:`check_password` verifies a login/password pair through PAM when the
  ``python-pam`` module (or the ``pam`` CLI) is available; otherwise it refuses
  password auth (fail-closed).
* :func:`authorized_keys_for` reads ``~user/.ssh/authorized_keys`` so the
  broker can authorise public keys exactly like OpenSSH does.
"""

from __future__ import annotations

import logging
import os
try:
    import pwd
except ImportError:  # Windows: server is Linux-only; guard for test collection.
    pwd = None

log = logging.getLogger("rd.auth")

# Sentinel: when ``pwd`` is None (Windows / a non-POSIX test runner) the
# server-side user/authorized_keys helpers cannot work -- the server is
# Linux-only by design. Rather than crashing with ``AttributeError:
# 'NoneType' object has no attribute 'getpwnam'`` we fail with a clear,
# typed error the caller (broker / tests) can branch on. The helpers below
# all funnel through :func:`_require_pwd` so the contract is uniform.
SERVER_IS_LINUX_ONLY = RuntimeError("server-side user lookup requires POSIX pwd (Linux-only)")


def _require_pwd():
    """Raise :data:`SERVER_IS_LINUX_ONLY` when the ``pwd`` module is absent.

    On a real Linux server ``pwd`` is always importable; on Windows / a
    headless test runner it is not, and the user/authorized_keys helpers
    must refuse explicitly instead of dereferencing ``None``.
    """
    if pwd is None:
        raise SERVER_IS_LINUX_ONLY

try:
    import pam as _pam  # python-pam

    _HAVE_PAM = True
except Exception:  # pragma: no cover
    _pam = None
    _HAVE_PAM = False


def user_exists(username: str) -> bool:
    """Return True if ``username`` is a real local POSIX account.

    On non-POSIX hosts (``pwd`` unavailable) this raises
    :data:`SERVER_IS_LINUX_ONLY` rather than returning a misleading
    ``False``: the server cannot authorise anyone without the user DB, so
    callers should treat the platform as unsupported, not the user as
    absent. The broker maps this to an auth refusal.
    """
    _require_pwd()
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def check_password(username: str, password: str, service: str = "login") -> bool:
    """Return True if ``password`` is valid for ``username`` via PAM."""
    try:
        if not user_exists(username):
            return False
    except RuntimeError as exc:
        # Non-POSIX host: server is Linux-only; refuse password auth.
        log.error("password auth unavailable on non-POSIX host: %s", exc)
        return False
    if _HAVE_PAM:
        try:
            p = _pam.pam()
            ok = p.authenticate(username, password, service=service)
            if not ok:
                log.info("PAM auth failed for %s: %s", username, p.reason)
            return bool(ok)
        except Exception as exc:  # pragma: no cover
            log.warning("PAM error: %s", exc)
            return False
    log.error("python-pam not installed; password auth unavailable")
    return False


def warn_if_pam_unavailable(*, allow_password: bool, pam_service: str = "login") -> None:
    """Log a clear startup warning when password auth is on but PAM is missing.

    Surfaces the root cause of "every password is rejected" immediately at
    server start, instead of only on the first (opaque) denied login attempt.
    Call this once from the server entry point after the config is loaded.

    ``python-pam`` reads ``/etc/shadow``, so even when it IS installed the
    server must run as root (or with the running user in the ``shadow`` group)
    — that requirement is hinted in the message too.
    """
    if not allow_password:
        return
    if _HAVE_PAM:
        return
    log.error(
        "allow_password=true but python-pam is unavailable — password "
        "authentication will reject EVERY login. Fix: install python-pam "
        "(pip install 'python-pam>=2.0.2', or your distro's python3-pam) and "
        "run rd-server as root / via the systemd unit (User=root), or add the "
        "running user to the 'shadow' group — PAM reads /etc/shadow. "
        "(pam_service=%r)", pam_service,
    )


def warn_if_privileges_insufficient(
    *, allow_password: bool, run_as_user: bool, pam_service: str = "login",
) -> None:
    """Warn when password auth / user-session drop are on but we lack root.

    ``allow_password=True`` routes logins through PAM, which reads
    ``/etc/shadow``; ``run_as_user=True`` drops privileges to each target user
    via ``setuid``. Both need root (or membership in the ``shadow`` group for
    the password path). Starting without those privileges doesn't crash — the
    server comes up — but every password login is silently rejected and no
    per-user session can be demoted, which looks like "the server doesn't
    work". Surface the cause loudly at startup so the operator fixes the
    launch context rather than chasing client-side errors.
    """
    if not (allow_password or run_as_user):
        return
    # Only meaningful on POSIX (the server is Linux-only anyway).
    if not hasattr(os, "geteuid"):
        return
    if os.geteuid() == 0:
        return  # root: PAM + setuid both available.
    # Non-root: is the 'shadow' group an option for the password path?
    in_shadow = False
    try:
        import grp
        groups = [g for g in os.getgroups()]
        try:
            shadow_gid = grp.getgrnam("shadow").gr_gid
            in_shadow = shadow_gid in groups
        except KeyError:
            pass
    except (ImportError, OSError):  # pragma: no cover
        pass
    if in_shadow and not run_as_user:
        return  # shadow group covers password auth; no setuid requested.
    reasons = []
    if allow_password and not in_shadow:
        reasons.append(
            "password authentication (PAM reads /etc/shadow)"
        )
    if run_as_user:
        reasons.append("per-user session privilege drop (setuid)")
    log.warning(
        "rd-server is running WITHOUT root, but the config enables %s. "
        "These features need root: start the server via the systemd unit "
        "(User=root), or `sudo rd-server`, or — for password auth only — add "
        "the running user to the 'shadow' group (`sudo usermod -aG shadow "
        "$USER`), or disable the features (allow_password=false, "
        "run_as_user=false). Otherwise logins/sessions will silently fail. "
        "(pam_service=%r)", " and ".join(reasons), pam_service,
    )


def authorized_keys_for(username: str) -> list[str]:
    """Return the raw OpenSSH public-key lines from the user's authorized_keys."""
    _require_pwd()
    try:
        rec = pwd.getpwnam(username)
    except KeyError:
        return []
    path = os.path.join(rec.pw_dir, ".ssh", "authorized_keys")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = []
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    lines.append(line)
            return lines
    except OSError as exc:  # pragma: no cover
        log.warning("cannot read authorized_keys for %s: %s", username, exc)
        return []
