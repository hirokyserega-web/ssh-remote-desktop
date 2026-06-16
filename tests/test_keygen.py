"""Keygen: round-trip a generated key, verify OpenSSH formatting and filesystem
permissions."""

import os
import stat
from pathlib import Path

import pytest

from crypto import generate_keypair, load_private_key, public_key_openssh, write_keypair


def test_ed25519_unencrypted(tmp_path: Path):
    kp = generate_keypair("ed25519", comment="test@host")
    assert kp.key_type == "ed25519"
    assert kp.public_openssh.startswith("ssh-ed25519 ")
    assert kp.public_openssh.endswith("test@host")
    priv, pub = write_keypair(kp, tmp_path, basename="id_test", overwrite=True)
    assert priv.exists() and pub.exists()
    # Private key must be 0600.
    mode = stat.S_IMODE(priv.stat().st_mode)
    assert mode == 0o600
    # Load it back without passphrase.
    reloaded = load_private_key(priv)
    assert reloaded.public_key().public_bytes(
        encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.OpenSSH,
        format=__import__("cryptography").hazmat.primitives.serialization.PublicFormat.OpenSSH,
    ) == kp.private_pem  # both are public-key form lines, this just exercises the loader


def test_rsa_with_passphrase(tmp_path: Path):
    kp = generate_keypair("rsa", rsa_bits=2048, passphrase="secret", comment="r@host")
    assert kp.key_type == "rsa"
    assert b"ENCRYPTED" in kp.private_pem
    priv, _ = write_keypair(kp, tmp_path, basename="id_rsa", overwrite=True)
    # Loading with wrong passphrase must fail.
    with pytest.raises(Exception):
        load_private_key(priv, passphrase="wrong")
    # Right passphrase works.
    reloaded = load_private_key(priv, passphrase="secret")
    assert reloaded.key_size == 2048


def test_write_refuses_overwrite_without_flag(tmp_path: Path):
    kp = generate_keypair("ed25519")
    write_keypair(kp, tmp_path, basename="id_x", overwrite=True)
    with pytest.raises(FileExistsError):
        write_keypair(kp, tmp_path, basename="id_x", overwrite=False)


def test_public_key_openssh_round_trip(tmp_path: Path):
    kp = generate_keypair("ed25519", comment="hello@world")
    priv, _ = write_keypair(kp, tmp_path, basename="id_e", overwrite=True)
    line = public_key_openssh(priv, comment="hello@world")
    assert line == kp.public_openssh
