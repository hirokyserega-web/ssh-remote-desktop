"""Server entry point.

Run with::

    python -m server [--config server.toml] [--port 2222] [--backend auto]

or, after building, the standalone ``rd-server`` executable.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from common.config import load_server_config
from .broker import Broker


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rd-server", description="SSH remote desktop server")
    p.add_argument("--config", help="path to server config (TOML/JSON)")
    p.add_argument("--host", help="bind address")
    p.add_argument("--port", type=int, help="listen port")
    p.add_argument("--backend", choices=["auto", "x11", "wayland"], help="force display backend")
    p.add_argument("--host-key", dest="host_key", help="SSH host key path")
    p.add_argument("--max-sessions", dest="max_sessions", type=int)
    p.add_argument("--idle-timeout", dest="idle_timeout", type=int)
    p.add_argument("--codec", choices=["h264", "h265", "jpeg", "webp"])
    p.add_argument("--fps", type=int)
    p.add_argument("--shared-dir", dest="shared_dir")
    p.add_argument("--no-clipboard", dest="clipboard_enabled", action="store_false", default=None)
    p.add_argument("--no-files", dest="files_enabled", action="store_false", default=None)
    p.add_argument("--log-level", dest="log_level")
    p.add_argument("--log-file", dest="log_file")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    overrides = {k: v for k, v in vars(args).items() if k != "config" and v is not None}
    cfg = load_server_config(args.config, overrides)

    handlers = [logging.StreamHandler(sys.stderr)]
    if cfg.log_file:
        handlers.append(logging.FileHandler(cfg.log_file))
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )

    broker = Broker(cfg)
    try:
        asyncio.run(broker.serve_forever())
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
