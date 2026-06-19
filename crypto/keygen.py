"""In-app SSH key generation, storage and ``authorized_keys`` helpers.

Implemented on top of the ``cryptography`` package so it works without any SSH
CLI tools installed -- the client bundles this and can mint keys from its GUI.
Supports Ed25519 (default) and RSA, optional passphrase protection, OpenSSH
private/public formats, and a helper that produces the exact line to drop into
a server's ``authorized_keys``.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives import hashes

KeyType = Literal["ed25519", "rsa"]


@dataclass
class KeyPair:
    """A generated key pair plus its serialized OpenSSH forms."""

    key_type: KeyType
    private_pem: bytes          # OpenSSH-format private key (optionally encrypted)
    public_openssh: str         # single-line "ssh-ed25519 AAAA... comment"
    comment: str

    def authorized_keys_line(self) -> str:
        return self.public_openssh

    @property
    def fingerprint(self) -> str:
        """OpenSSH-style SHA256 fingerprint (``SHA256:base64``).

        Computed from the raw public-key blob embedded in
        :attr:`public_openssh` (the base64 segment after the key type). Matches
        ``ssh-keygen -lf`` and :meth:`asyncssh.SSHKey.get_fingerprint`.
        """
        return public_key_fingerprint(self.public_openssh)


def generate_keypair(
    key_type: KeyType = "ed25519",
    *,
    rsa_bits: int = 3072,
    passphrase: str | None = None,
    comment: str = "",
) -> KeyPair:
    """Generate a fresh key pair.

    :param key_type: ``"ed25519"`` (recommended) or ``"rsa"``.
    :param rsa_bits: modulus size when ``key_type == "rsa"``.
    :param passphrase: when set, the private key is encrypted at rest.
    :param comment: appended to the public key line (e.g. ``user@host``).
    """
    if key_type == "ed25519":
        priv = ed25519.Ed25519PrivateKey.generate()
    elif key_type == "rsa":
        priv = rsa.generate_private_key(public_exponent=65537, key_size=rsa_bits)
    else:  # pragma: no cover - guarded by typing
        raise ValueError(f"unsupported key type: {key_type}")

    enc: serialization.KeySerializationEncryption
    if passphrase:
        enc = serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
    else:
        enc = serialization.NoEncryption()

    private_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=enc,
    )

    pub = priv.public_key()
    public_openssh = pub.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    if comment:
        public_openssh = f"{public_openssh} {comment}"

    return KeyPair(
        key_type=key_type,
        private_pem=private_pem,
        public_openssh=public_openssh,
        comment=comment,
    )


def public_key_fingerprint(openssh_line: str) -> str:
    """Compute the OpenSSH-style SHA256 fingerprint of a public key.

    Takes an OpenSSH public key line like "ssh-ed25519 AAAA... comment",
    parses the base64 blob, and returns "SHA256:base64(sha256(blob))".
    """
    parts = openssh_line.split()
    if len(parts) < 2:
        raise ValueError("Invalid OpenSSH public key line")
    blob = parts[1]
    from base64 import b64decode
    blob_bytes = b64decode(blob)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(blob_bytes)
    digest_value = digest.finalize()
    from base64 import b64encode
    # OpenSSH / asyncssh strip the base64 padding '=' so the fingerprint is
    # compact; match that exactly or comparisons against `ssh-keygen -lf`
    # and asyncssh's get_fingerprint() fail.
    return f"SHA256:{b64encode(digest_value).decode('ascii').rstrip('=')}"


def write_keypair(
    keypair: KeyPair,
    directory: str | os.PathLike,
    *,
    basename: str | None = None,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    """Write the key pair to ``directory`` with safe permissions.

    Returns ``(private_path, public_path)``. The private key is written with
    mode 0600 (owner read/write only), matching OpenSSH's expectations.
    """
    directory = Path(os.path.expanduser(str(directory)))
    directory.mkdir(parents=True, exist_ok=True)
    name = basename or f"id_{keypair.key_type}"
    priv_path = directory / name
    pub_path = directory / f"{name}.pub"

    if not overwrite and (priv_path.exists() or pub_path.exists()):
        raise FileExistsError(f"key files already exist at {priv_path}")

    # Write private key 0600.
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, keypair.private_pem)
    finally:
        os.close(fd)
    os.chmod(priv_path, stat.S_IRUSR | stat.S_IWUSR)

    pub_path.write_text(keypair.public_openssh + "\n", encoding="ascii")
    os.chmod(pub_path, 0o644)
    return priv_path, pub_path


def load_private_key(path: str | os.PathLike, passphrase: str | None = None):
    """Load an OpenSSH/PEM private key for use with asyncssh/paramiko.

    Returns the ``cryptography`` private-key object; callers that need an
    asyncssh key can also just pass the file path to asyncssh directly.
    """
    data = Path(os.path.expanduser(str(path))).read_bytes()
    pw = passphrase.encode("utf-8") if passphrase else None
    return serialization.load_ssh_private_key(data, password=pw)


def public_key_openssh(private_key_path: str | os.PathLike, passphrase: str | None = None,
                       comment: str = "") -> str:
    """Derive the OpenSSH public-key line from an existing private key file."""
    priv = load_private_key(private_key_path, passphrase)
    line = priv.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return f"{line} {comment}".strip()


def authorized_keys_line(public_openssh: str, *, options: str = "") -> str:
    """Build an ``authorized_keys`` line, optionally with key options."""
    line = public_openssh.strip()
    return f"{options} {line}".strip() if options else line
