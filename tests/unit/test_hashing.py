from __future__ import annotations

from app.utils.hashing import content_hash, normalize_text


def test_normalize_text_collapses_whitespace_and_lowercases() -> None:
    assert normalize_text("  Hello   World  ") == "hello world"


def test_content_hash_is_deterministic() -> None:
    assert content_hash("Acme Corp", "Senior Engineer") == content_hash("Acme Corp", "Senior Engineer")


def test_content_hash_ignores_whitespace_and_case_differences() -> None:
    assert content_hash("Acme Corp", "Senior Engineer") == content_hash("  acme   corp  ", "SENIOR ENGINEER")


def test_content_hash_differs_for_different_input() -> None:
    assert content_hash("Acme Corp") != content_hash("Other Corp")
