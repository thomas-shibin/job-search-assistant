from __future__ import annotations

import re

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(value: str, *, max_length: int = 100) -> str:
    """Make a string safe to use as a Windows file/directory name."""
    if not value or not value.strip():
        raise ValueError("cannot sanitize an empty filename")

    cleaned = _INVALID_CHARS.sub("", value)
    cleaned = _WHITESPACE.sub("-", cleaned.strip())
    cleaned = cleaned.strip(". ")

    if not cleaned:
        raise ValueError(f"filename '{value}' sanitizes to an empty string")

    if cleaned.upper() in _RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"

    return cleaned[:max_length]
