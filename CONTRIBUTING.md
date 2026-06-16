# Contributing

## Code style

- Python 3.11+ syntax (`from __future__ import annotations`, structural
  pattern matching is fine where it helps).
- One module per concept; no god-files. See `file 'common'`,
  `file 'server/backend'`, `file 'client'`.
- Logging through `logging.getLogger("rd.*")` — never `print` from library
  code (only from CLI `__main__`).
- Public API of every module exposed via `__all__` where it helps.
- Keep