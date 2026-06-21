"""Regression test for the ``client=`` kwarg crash on the pinned asyncssh floor.

Symptom (user log):
    connection failed: SSHClientConnectionOptions.prepare() got an unexpected
    keyword argument 'client'

Root cause: ``client/transport.py:_connect_options`` built the connect-options
dict with ``opts["client"] = TofuClient(...)``. asyncssh's ``connect()`` funnels
unknown kwargs into ``SSHClientConnectionOptions.prepare()``, which on the
pinned floor (``asyncssh>=2.23``) has *no* ``client`` parameter — only the
callable-form ``client_factory``. So every connect attempt raised TypeError
before the TCP connection was even attempted, and the transport loop crashed
after the retry budget.

The fix is to pass ``client_factory`` (a callable returning an SSHClient)
instead of the instance directly. This test pins that:

* ``_connect_options`` returns ``client_factory`` (a callable), NOT ``client``
* the factory actually returns a ``TofuClient``
* calling the factory twice yields independent instances (per-connection state)
* the resulting options dict is accepted by ``SSHClientConnectionOptions``
  on the installed asyncssh (no ``unexpected keyword argument 'client'``)
"""
from __future__ import annotations

import inspect

import asyncssh

from common.config import ClientConfig
from client.hostkeys import TofuClient
from client.transport import Transport


def _make_transport(tmp_path) -> Transport:
    # Use agent auth so asyncssh doesn't try to read a (non-existent) key file
    # during SSHClientConnectionOptions.prepare — that would mask the real
    # assertion (the `client=`/`client_factory` shape) behind a FileNotFoundError.
    cfg = ClientConfig(
        known_hosts=str(tmp_path / "kh"),
        user="u",
        auth="agent",
        host="127.0.0.1",
        port=2299,
    )
    return Transport(cfg)


def test_connect_options_uses_client_factory_not_client(tmp_path):
    """The dict must contain `client_factory`, never the bare `client` key."""
    t = _make_transport(tmp_path)
    opts = t._connect_options()
    assert "client_factory" in opts, "must use client_factory (callable), not client= (instance)"
    assert "client" not in opts, (
        "the `client=` instance kwarg crashes on asyncssh>=2.23,<newer: "
        "SSHClientConnectionOptions.prepare() has no `client` parameter"
    )


def test_client_factory_returns_a_tofu_client(tmp_path):
    """The factory must produce a TofuClient instance (the thing asyncssh
    drives validate_host_public_key on)."""
    t = _make_transport(tmp_path)
    opts = t._connect_options()
    inst = opts["client_factory"]()
    assert isinstance(inst, TofuClient)


def test_client_factory_yields_independent_instances(tmp_path):
    """Each call returns a fresh TofuClient — per-connection state must not be
    shared, or connection_made's host/port capture would leak across reconnects."""
    t = _make_transport(tmp_path)
    opts = t._connect_options()
    a = opts["client_factory"]()
    b = opts["client_factory"]()
    assert a is not b, "factory must create a new TofuClient per connection"


def test_connect_options_accepted_by_ssh_options_prepare(tmp_path):
    """The options dict must round-trip through SSHClientConnectionOptions
    on the installed asyncssh — i.e. no `unexpected keyword argument` error.

    This is the exact crash the user saw: prepare() rejected `client=`. With
    `client_factory` it must construct cleanly.
    """
    t = _make_transport(tmp_path)
    opts = t._connect_options()
    # SSHClientConnectionOptions copies kwargs from the dict on construction;
    # any unknown kwarg raises TypeError here. If this passed before the fix
    # on the installed asyncssh, the installed version already accepts `client`,
    # but the test still pins that we don't regress back to the broken shape.
    try:
        asyncssh.SSHClientConnectionOptions(**opts)
    except TypeError as exc:
        # If the installed asyncssh still rejects the dict, that's a real
        # regression — surface the parameter list so the failure is actionable.
        sig = inspect.signature(asyncssh.SSHClientConnectionOptions.prepare)
        params = ", ".join(sig.parameters)
        raise AssertionError(
            f"SSHClientConnectionOptions rejected _connect_options output: {exc}\n"
            f"prepare() parameters: {params}"
        ) from exc
