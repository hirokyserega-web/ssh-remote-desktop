"""Tests for backend auto-detection (C4): wayland is experimental, so 'auto'
prefers x11 (Xvfb) when available and logs a prominent warning otherwise."""
from __future__ import annotations

import logging
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server import backend as be
from server.backend import detect_backend_kind


def _set_xvfb(monkeypatch, available: bool):
    """Make detect_backend_kind's Xvfb probe deterministic across CI/sandbox."""
    monkeypatch.setattr(
        be.shutil, "which",
        (lambda name: "/usr/bin/Xvfb" if name == "Xvfb" else None) if available
        else (lambda name: None),
    )


def test_forced_backend_wins_and_no_warning(monkeypatch, caplog):
    _set_xvfb(monkeypatch, available=True)
    with caplog.at_level(logging.WARNING, logger="rd.backend"):
        assert detect_backend_kind({"XDG_SESSION_TYPE": "wayland"}, forced="x11") == "x11"
    assert not any("EXPERIMENTAL" in r.message for r in caplog.records)


def test_auto_prefers_x11_when_xvfb_available_under_wayland_session(monkeypatch, caplog):
    """C4: on a Wayland desktop, auto should pick the working x11 (Xvfb)
    backend, not the experimental wayland placeholder, when Xvfb is installed."""
    _set_xvfb(monkeypatch, available=True)
    with caplog.at_level(logging.WARNING, logger="rd.backend"):
        kind = detect_backend_kind({"XDG_SESSION_TYPE": "wayland"})
    assert kind == "x11"
    assert any("EXPERIMENTAL" in r.message for r in caplog.records)


def test_auto_falls_back_to_wayland_when_no_xvfb(monkeypatch, caplog):
    _set_xvfb(monkeypatch, available=False)
    with caplog.at_level(logging.WARNING, logger="rd.backend"):
        kind = detect_backend_kind({"XDG_SESSION_TYPE": "wayland"})
    assert kind == "wayland"
    assert any("EXPERIMENTAL" in r.message for r in caplog.records)


def test_auto_x11_env_no_warning(monkeypatch, caplog):
    _set_xvfb(monkeypatch, available=False)
    with caplog.at_level(logging.WARNING, logger="rd.backend"):
        assert detect_backend_kind({"DISPLAY": ":1"}) == "x11"
    assert not any("EXPERIMENTAL" in r.message for r in caplog.records)


def test_auto_defaults_to_x11_without_signals(monkeypatch):
    _set_xvfb(monkeypatch, available=False)
    assert detect_backend_kind({}) == "x11"


def test_forced_wayland_keeps_wayland(monkeypatch, caplog):
    _set_xvfb(monkeypatch, available=True)
    # An explicit backend="wayland" is honoured as-is (no auto-flip, no warning).
    with caplog.at_level(logging.WARNING, logger="rd.backend"):
        assert detect_backend_kind({"XDG_SESSION_TYPE": "wayland"}, forced="wayland") == "wayland"
    assert not any("EXPERIMENTAL" in r.message for r in caplog.records)
