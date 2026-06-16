"""Shared protocol, framing and multiplexing primitives.

This package is imported by both the client and the server. It defines:

* :mod:`common.protocol` -- channel ids, message-type constants, protocol
  version and the wire-level frame layout.
* :mod:`common.framing`  -- a binary frame codec plus an async multiplexer that
  carries the logical channels (control / video / input / clipboard / files)
  over a single byte stream.
* :mod:`common.messages` -- small helpers to build the JSON control/input/
  clipboard messages described in the prompt.
* :mod:`common.logging_utils` -- a single ``setup_logging`` entry-point used by
  every executable so log formatting stays consistent.
"""

from .protocol import (
    PROTO_VERSION,
    Channel,
    FRAME_HEADER,
    FRAME_HEADER_SIZE,
    Flags,
)

__all__ = [
    "PROTO_VERSION",
    "Channel",
    "FRAME_HEADER",
    "FRAME_HEADER_SIZE",
    "Flags",
]
