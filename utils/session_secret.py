"""Create a stable local secret used to sign browser sessions."""
from __future__ import annotations

import os
from pathlib import Path
import secrets


def get_session_secret(configured_secret: str = "") -> str:
    if configured_secret.strip():
        return configured_secret.strip()
    runtime_dir = Path(__file__).resolve().parents[1] / ".runtime"
    secret_file = runtime_dir / "session-secret"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if secret_file.exists():
        value = secret_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
    except FileExistsError:
        return secret_file.read_text(encoding="utf-8").strip()
    return value
