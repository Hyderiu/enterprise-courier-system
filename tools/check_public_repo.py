"""Fail when files that must never enter the public repository are present."""
from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".html", ".css", ".js", ".json", ".yml",
    ".yaml", ".toml", ".ini", ".cfg", ".cmd", ".ps1", ".sh", ".example",
}
FORBIDDEN_NAMES = {
    ".env", ".publish-config.json", "courier.db", "courier.db-shm",
    "courier.db-wal", "demo.db", "test-public.db",
}
FORBIDDEN_SUFFIXES = {".pem", ".p12", ".pfx", ".sqlite", ".sqlite3"}
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
ASSIGNED_SECRET_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|CHECK_WORD|DEV_KEY|APPKEY|ACCESS_KEY)[A-Z0-9_]*)"
    r"[ \t]*=[ \t]*([^\s#][^\r\n]*)$"
)
LITERAL_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:secret|token|password|check_word|dev_key|appkey|access_key)\b"
    r"[ \t]*=[ \t]*['\"]([A-Za-z0-9_+/=-]{16,})['\"]"
)
PRIVATE_IPV4_PATTERN = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
LOCAL_ABSOLUTE_PATH_PATTERN = re.compile(r"(?i)\b[A-Z]:\\(?:Users|Desktop|Windows)\\")


def included_files():
    for path in ROOT.rglob("*"):
        if path.resolve() == Path(__file__).resolve():
            continue
        if not path.is_file() or any(part in IGNORED_DIRS for part in path.parts):
            continue
        yield path


def main() -> int:
    findings: list[str] = []
    for path in included_files():
        relative = path.relative_to(ROOT).as_posix()
        lower_name = path.name.lower()
        if lower_name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"forbidden file: {relative}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".env.example":
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        if PRIVATE_KEY_PATTERN.search(text):
            findings.append(f"private key marker: {relative}")
        if path.name.startswith(".env") and ASSIGNED_SECRET_PATTERN.search(text):
            findings.append(f"non-empty secret assignment: {relative}")
        elif LITERAL_SECRET_PATTERN.search(text):
            findings.append(f"hard-coded secret-like literal: {relative}")
        if PRIVATE_IPV4_PATTERN.search(text):
            findings.append(f"private network address: {relative}")
        if LOCAL_ABSOLUTE_PATH_PATTERN.search(text):
            findings.append(f"local absolute path: {relative}")
    if findings:
        print("Public repository check failed:")
        for item in sorted(set(findings)):
            print(f"- {item}")
        return 1
    print("Public repository check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
