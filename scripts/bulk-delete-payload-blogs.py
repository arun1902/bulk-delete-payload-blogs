#!/usr/bin/env python3
"""
CLI entry point for safe, resumable Payload blog-posts bulk deletion.

See scripts/lib/bulk_delete_payload_blogs.py for implementation details.
Defaults to DRY_RUN=true. Real deletion requires --execute and typing DELETE.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from bulk_delete_payload_blogs import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
