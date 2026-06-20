"""Tests for TOFU host-key verification (P0 3.4).

Covers the KnownHostsStore file format and the TofuClient policy:
accept known+matching, reject known+mismatch, ask on unknown, record on
acceptance, overwrite on explicit accept of a changed key.
"""

from __future__ import annotations

import asyncio

import asyncssh
import pytest


from client.hostkeys import (
    KnownHostsStore,
    TofuClient,
    HostKeyMismatch,
    HostKeyRejected,
    _key_blob_b64,
)


@pytest.fixture
def store(tmp_path):
    return KnownHostsStore(tmp_path / "rd_known_hosts")


@pytest.fixture
def ed25519_key():
    return asyncssh.generate_private_key("ssh-ed25519")


@pytest.fixture
def rsa_key():
    return asyncssh.generate_private_key("ssh-rsa", 2048)


@pytest.fixture
def key(ed25519_key):
    """A public SSH key in the form asyncssh passes to validate_host_public_key."""
    return ed25519_key.convert_to_public()


def _to_ssh_key(privkey) -> "asyncssh.SSHKey":
    """asyncssh SSHKey with .public_data and .get_fingerprint() for the store."""
    return privkey.convert_to_public()


def test_store_lookup_unknown(store):
    assert store.lookup("example.com", 22) is None


def test_store_add_and_lookup(store, ed25519_key):
    pub = _to_ssh_key(ed25519_key)
    store.add("example.com", 22, pub)
    entry = store.lookup("example.com", 22)
    assert entry is not None
    assert entry.host == "example.com"
    assert entry.port == 22
    assert entry.fingerprint == pub.get_fingerprint()
    assert entry.key_blob_b64 == _key_blob_b64(pub)


def test_store_replace_overwrites(store, ed25519_key, rsa_key):
    pub1 = _to_ssh_key(ed25519_key)
    pub2 = _to_ssh_key(rsa_key)
    store.add("example.com", 22, pub1)
    store.replace("example.com", 22, pub2)
    entry = store.lookup("example.com", 22)
    assert entry is not None
    assert entry.fingerprint == pub2.get_fingerprint()
    assert entry.key_blob_b64 == _key_blob_b64(pub2)


def test_store_append_format(store, ed25519_key):
    pub = _to_ssh_key(ed25519_key)
    store.add("example.com", 2222, pub)
    content = store.path.read_text()
    parts = content.strip().split()
    assert parts[0] == "example.com"
    assert parts[1] == "2222"
    assert parts[2] == _key_blob_b64(pub)
    assert parts[3] == pub.get_fingerprint()


def test_tofu_unknown_accepted_then_known(key, store):
    """Unknown key + accept → recorded; second call accepts silently."""

    async def _run():
        store._load.cache_clear() if hasattr(store._load, "cache_clear") else None
        asked = []
        async def ask(host, port, fp, first_time, old_fingerprint=None):
            asked.append((host, port, fp, first_time))
            return True
        client = TofuClient(store, ask)
        # Unknown: should ask and accept.
        await client.validate_host_public_key("myhost", "1.2.3.4", 22, key)
        assert len(asked) == 1
        assert asked[0][3] is True  # first_time
        # Now known: should accept without asking.
        asked.clear()
        await client.validate_host_public_key("myhost", "1.2.3.4", 22, key)
        assert len(asked) == 0

    asyncio.run(_run())


def test_tofu_unknown_rejected_raises(key, store):
    """Unknown key + reject → HostKeyRejected."""

    async def _run():
        async def ask(host, port, fp, first_time, old_fingerprint=None):
            return False
        client = TofuClient(store, ask)
        with pytest.raises(HostKeyRejected):
            await client.validate_host_public_key("myhost", "1.2.3.4", 22, key)

    asyncio.run(_run())


def test_tofu_mismatch_rejected_raises(key, store):
    """Known key + different key presented + reject → HostKeyMismatch."""

    async def _run():
        other = asyncssh.generate_private_key("ssh-ed25519", "test-other")
        store.add("myhost", 22, key)  # record first key
        async def ask(host, port, fp, first_time, old_fingerprint=None):
            return False
        client = TofuClient(store, ask)
        with pytest.raises(HostKeyMismatch):
            await client.validate_host_public_key("myhost", "1.2.3.4", 22, other)

    asyncio.run(_run())


def test_tofu_mismatch_accepted_overwrites(key, store):
    """Known key + different key + accept → store overwritten."""

    async def _run():
        other = asyncssh.generate_private_key("ssh-ed25519", "test-other")
        store.add("myhost", 22, key)
        async def ask(host, port, fp, first_time, old_fingerprint=None):
            assert first_time is False  # mismatch, not first-time
            return True
        client = TofuClient(store, ask)
        await client.validate_host_public_key("myhost", "1.2.3.4", 22, other)
        # Store should now have the new key.
        entry = store.lookup("myhost", 22)
        assert entry is not None
        assert entry.fingerprint == other.get_fingerprint()

    asyncio.run(_run())


def test_tofu_unknown_accepted_not_remembered_not_persisted(key, store):
    """accept + remember=False → trusted this connect, NOT persisted; next call asks again."""

    async def _run():
        asked = []

        async def ask(host, port, fp, first_time, old_fingerprint=None):
            asked.append(first_time)
            return (True, False)  # accept, but do not remember

        client = TofuClient(store, ask)
        # First call: unknown, accepted without remembering.
        assert await client.validate_host_public_key("myhost", "1.2.3.4", 22, key) is True
        # Key must NOT have been written to the store.
        assert store.lookup("myhost", 22) is None
        # Second call: still unknown (not persisted), so it asks again.
        assert await client.validate_host_public_key("myhost", "1.2.3.4", 22, key) is True
        assert asked == [True, True]

    asyncio.run(_run())


def test_tofu_mismatch_accepted_not_remembered_not_overwritten(key, store):
    """mismatch + accept + remember=False → trusted this connect, store keeps old key."""

    async def _run():
        other = asyncssh.generate_private_key("ssh-ed25519", "test-other")
        store.add("myhost", 22, key)  # original key on record

        async def ask(host, port, fp, first_time, old_fingerprint=None):
            assert first_time is False
            return (True, False)  # accept changed key, but do not remember

        client = TofuClient(store, ask)
        assert await client.validate_host_public_key("myhost", "1.2.3.4", 22, other) is True
        # Store must still hold the ORIGINAL key (not overwritten).
        entry = store.lookup("myhost", 22)
        assert entry is not None
        assert entry.fingerprint == key.get_fingerprint()

    asyncio.run(_run())