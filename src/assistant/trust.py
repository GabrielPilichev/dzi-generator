"""Trust levels for assistant tools.

A skill binds each tool to one trust level. The agent loop enforces it: READ
tools run freely, CONFIRM tools run only after the operator says yes, and
FORBIDDEN tools are never offered and are refused if called.
"""

from __future__ import annotations

import enum


class TrustLevel(str, enum.Enum):
    """How much the agent may do with a tool without asking."""

    READ = "read"  # runs without asking; must not change anything
    CONFIRM = "confirm"  # writes a proposal; runs only after the operator says yes
    FORBIDDEN = "forbidden"  # never offered to the model; a call is refused and logged
