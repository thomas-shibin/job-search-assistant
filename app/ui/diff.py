"""Word-level inline HTML diff for the resume-diff view (Milestone 9). Server-
rendered with the stdlib's `difflib` rather than a client-side diff library
(docs/ARCHITECTURE.md floated "a small client-side diff library", but a
server-side diff needs no extra JS dependency and keeps the diff computation
next to the data it's diffing).
"""
from __future__ import annotations

import difflib

from markupsafe import Markup, escape


def inline_word_diff_html(before: str | None, after: str) -> Markup:
    if not before or before == after:
        return Markup(escape(after))

    before_words = before.split()
    after_words = after.split()
    matcher = difflib.SequenceMatcher(a=before_words, b=after_words)

    parts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(str(escape(" ".join(after_words[j1:j2]))))
        elif tag == "insert":
            parts.append(f"<ins>{escape(' '.join(after_words[j1:j2]))}</ins>")
        elif tag == "delete":
            parts.append(f"<del>{escape(' '.join(before_words[i1:i2]))}</del>")
        elif tag == "replace":
            parts.append(f"<del>{escape(' '.join(before_words[i1:i2]))}</del>")
            parts.append(f"<ins>{escape(' '.join(after_words[j1:j2]))}</ins>")

    return Markup(" ".join(parts))
