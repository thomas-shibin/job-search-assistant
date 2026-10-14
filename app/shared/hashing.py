from __future__ import annotations

import hashlib
import re

_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value.strip().lower())


def content_hash(*parts: str) -> str:
    """Deterministic, whitespace/case-insensitive hash used for job fingerprints,
    description-change detection, and AI-result cache keys.
    """
    normalized = "|".join(normalize_text(p) for p in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
