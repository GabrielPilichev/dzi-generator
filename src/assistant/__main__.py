"""``python -m src.assistant`` entry point."""
from __future__ import annotations

import sys

from src.assistant.cli import main

if __name__ == "__main__":
    sys.exit(main())
