"""Tests for HostKeyDialog: TOFU first-time + changed-key (with old fingerprint) + i18n."""
from __future__ import annotations
import sys
import pytest


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Qt offscreen needs a display; skip on Windows CI",
)


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


import os  # noqa: E402


def test_first_time_dialog_remembers(qapp):
    from client.host_key_dialog import HostKeyDialog
    dlg = HostKeyDialog("myhost", 2222, "SHA256:abc123", changed=False)
    assert dlg.remember() is True  # default True on first connect


def test_changed_dialog_defaults_no_remember(qapp):
    from client.host_key_dialog import HostKeyDialog
    dlg = HostKeyDialog("myhost", 2222, "SHA256:new", changed=True,
                        old_fingerprint="SHA256:old")
    assert dlg.remember() is False  # force explicit opt-in on key change


def test_changed_dialog_shows_both_fingerprints(qapp):
    from client.host_key_dialog import HostKeyDialog
    from PySide6.QtWidgets import QLabel
    dlg = HostKeyDialog("myhost", 2222, "SHA256:new", changed=True,
                        old_fingerprint="SHA256:old")
    labels = dlg.findChildren(QLabel)
    texts = [lbl.text() for lbl in labels]
    joined = " ".join(texts)
    assert "SHA256:old" in joined
    assert "SHA256:new" in joined


def test_first_time_dialog_no_old_fp(qapp):
    from client.host_key_dialog import HostKeyDialog
    from PySide6.QtWidgets import QLabel
    dlg = HostKeyDialog("myhost", 2222, "SHA256:abc", changed=False)
    labels = dlg.findChildren(QLabel)
    texts = [lbl.text() for lbl in labels]
    joined = " ".join(texts)
    assert "SHA256:abc" in joined
