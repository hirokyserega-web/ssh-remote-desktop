"""Tests for crypto/keygen: KeyPair.fingerprint + public_key_fingerprint helper.

Both must produce the OpenSSH-style SHA256 fingerprint and agree with each
other and with asyncssh's SSHKey.get_fingerprint() for the same public blob.
"""
from __future__ import annotations

import base64

import asyncssh

from crypto import generate_keypair, public_key_fingerprint


def test_keypair_fingerprint_matches_helper():
    kp = generate_keypair(key_type="ed25519", comment="t")
    assert kp.fingerprint == public_key_fingerprint(kp.public_openssh)


def test_fingerprint_format():
    kp = generate_keypair(key_type="ed25519")
    assert kp.fingerprint.startswith("SHA256:")
    # base64 of a 32-byte SHA256 digest -> 44 chars (no padding stripped in
    # OpenSSH format, but base64encode keeps the '=' padding).
    body = kp.fingerprint[len("SHA256:"):]
    assert len(body) >= 43


def test_fingerprint_matches_asyncssh_for_same_blob():
    """For the same raw public key blob, our SHA256 matches asyncssh's."""
    # Generate via asyncssh (the reference implementation used by the server
    # host key path) and compare fingerprints for the same blob.
    priv = asyncssh.generate_private_key("ssh-ed25519")
    pub = priv.convert_to_public()
    blob = pub.public_data
    # Reconstruct an OpenSSH single-line form: "ssh-ed25519 <base64-blob>".
    openssh_line = f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')}"
    ours = public_key_fingerprint(openssh_line)
    theirs = pub.get_fingerprint()
    assert ours == theirs, f"{ours} != {theirs}"


def test_fingerprint_invalid_line_raises():
    import pytest
    with pytest.raises(ValueError):
        public_key_fingerprint("not-a-key")
