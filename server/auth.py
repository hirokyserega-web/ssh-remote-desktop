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

try:
    import pam as _pam  # python-pam

    _HAVE_PAM = True
except Exception:  # pragma: no cover
    _pam = None
    _HAVE_PAM = False


def user_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def check_password(username: str, password: str, service: str = "login") -> bool:
    """Return True if ``password`` is valid for ``username`` via PAM."""
    if not user_exists(username):
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


def authorized_keys_for(username: str) -> list[str]:
    """Return the raw OpenSSH public-key lines from the user's authorized_keys."""
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
