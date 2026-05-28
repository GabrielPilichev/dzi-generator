#!/usr/bin/env python3
"""Compatibility entry point for confirmed DZI image asset file validation."""

from __future__ import annotations

try:
    from .validate_confirmed_dzi_image_files import main
except ImportError:
    from validate_confirmed_dzi_image_files import main


if __name__ == "__main__":
    raise SystemExit(main())
