from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def strip_html_tags(value: str) -> str:
    """Minimal HTML-to-plain-text conversion for job-posting descriptions
    fetched from source APIs (Greenhouse/Lever return HTML). Not a full HTML
    parser -- just enough to make posting text readable: block-level tags
    become line breaks, all other tags are dropped, and entities are decoded.
    """
    if not value:
        return ""
    text = re.sub(r"(?i)<(p|div|br|li|/li|h[1-6])\s*/?>", "\n", value)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()
