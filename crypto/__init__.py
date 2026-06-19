"""SSH key generation and storage helpers (built into the client)."""

from .keygen import (
    KeyPair,
    generate_keypair,
    load_private_key,
    public_key_openssh,
    authorized_keys_line,
    write_keypair,
    public_key_fingerprint,
)

__all__ = [
    "KeyPair",
    "generate_keypair",
    "load_private_key",
    "public_key_openssh",
    "authorized_keys_line",
    "write_keypair",
    "public_key_fingerprint",
]
