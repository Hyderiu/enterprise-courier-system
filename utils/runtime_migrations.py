"""Public-edition runtime migration hook.

The private deployment contains historical migrations tied to organization
endpoints and live carrier switches.  They are deliberately absent here: a
public checkout must never enable SSO or real-order capabilities by itself.
"""
from __future__ import annotations

from pathlib import Path


def apply_runtime_migrations(root: Path) -> None:
    """Keep the hook for compatibility without changing public settings."""
    del root
