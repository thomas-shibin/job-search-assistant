"""Comparing two resume versions line by line (app/modules/resumes/versions.py)."""
from __future__ import annotations

from app.modules.resumes import versions


def test_changed_added_removed_and_moved_lines():
    old = ["Jordan Lee", "jordan@example.com", "Experience", "Organised hiring for 4 plants", "References on request"]
    new = ["Jordan Lee", "Experience", "Organized hiring for 4 plants", "Python | SQL", "jordan@example.com"]

    result = versions.compare(old, new, only_changes=False)

    kinds = [row.kind for row in result.rows]
    assert kinds.count("changed") == 1 and result.changed == 1
    changed = next(row for row in result.rows if row.kind == "changed")
    assert "<del>Organised</del>" in changed.before and "<ins>Organized</ins>" in changed.after
    assert result.moved == 1 and "moved_from" in kinds  # the email went from the top to the bottom (or the rest up)
    assert result.added == 1 and result.removed == 1  # "Python | SQL" in, the references out
    assert not result.same


def test_only_the_changes_with_a_line_around_each():
    old = [f"line {n}" for n in range(20)]
    new = list(old)
    new[10] = "line ten, reworded"

    rows = versions.compare(old, new).rows

    assert [row.kind for row in rows] == ["skipped", "same", "removed", "added", "same", "skipped"]
    assert rows[0].skipped == 9 and rows[-1].skipped == 8
    assert versions.compare(old, list(old)).same
