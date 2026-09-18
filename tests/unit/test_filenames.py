from __future__ import annotations

import pytest

from app.utils.filenames import sanitize_filename


def test_removes_invalid_characters() -> None:
    assert sanitize_filename("Senior/Backend: Engineer?") == "SeniorBackend-Engineer"


def test_collapses_whitespace_to_hyphen() -> None:
    assert sanitize_filename("Company   Name") == "Company-Name"


def test_empty_string_raises() -> None:
    with pytest.raises(ValueError):
        sanitize_filename("   ")


def test_reserved_windows_name_is_escaped() -> None:
    assert sanitize_filename("CON") == "_CON"


def test_truncates_to_max_length() -> None:
    result = sanitize_filename("a" * 200, max_length=50)
    assert len(result) == 50
