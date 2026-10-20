"""The theme menu's CSS: the dark colours are written twice -- once for the
device's dark setting (unless the visitor chose Light), once for an explicit
Dark choice -- and the two copies must not drift apart."""
from __future__ import annotations

import re

from app.core.paths import PROJECT_ROOT

CSS = (PROJECT_ROOT / "app" / "web" / "static" / "style.css").read_text()


def _declarations(block: str) -> list[str]:
    return [line.strip() for line in block.strip().splitlines() if line.strip()]


def test_dark_tokens_are_the_same_for_the_system_setting_and_the_dark_choice():
    system = re.search(r'@media \(prefers-color-scheme: dark\) \{\n  :root:not\(\[data-theme="light"\]\) \{\n(.*?)\n  \}\n\}',
                       CSS, re.S).group(1)
    chosen = re.search(r'\n:root\[data-theme="dark"\] \{\n(.*?)\n\}', CSS, re.S).group(1)
    assert "--bg:" in system
    assert _declarations(system) == _declarations(chosen)


def test_every_dark_override_also_applies_to_the_dark_choice():
    system_rules = re.findall(r'@media \(prefers-color-scheme: dark\) \{ :root:not\(\[data-theme="light"\]\) (.*?) \}\n', CSS)
    assert len(system_rules) == 2  # .fit-good and the chart colours
    for rule in system_rules:
        assert f':root[data-theme="dark"] {rule}' in CSS
    assert "@media (prefers-color-scheme: dark) { :root {" not in CSS  # nothing left that ignores the choice


def test_static_files_are_versioned_by_content(tmp_path, monkeypatch):
    """A changed app.js or style.css gets a new URL, so browsers don't keep running
    a cached old copy after a deploy (which once hid the theme menu's script)."""
    from app.web import templating

    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(templating, "_static_versions", {})
    (tmp_path / "app.js").write_text("one")
    first = templating.static_url("app.js")
    assert first.startswith("/static/app.js?v=") and templating.static_url("app.js") == first
    (tmp_path / "app.js").write_text("two -- a new deploy")
    import os
    os.utime(tmp_path / "app.js", (1, 1))
    assert templating.static_url("app.js") != first
    assert templating.static_url("missing.js") == "/static/missing.js"
