"""Small server-rendered SVG bar charts for the admin dashboard: no JavaScript
or chart library, so they work under the page's Content-Security-Policy and
print/zoom crisply. Colours come from CSS classes (`.series-1` ... in
style.css), so they follow the light/dark theme; each bar has a native
tooltip (`<title>`)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from markupsafe import Markup, escape

WIDTH = 720
HEIGHT = 220
_LEFT, _RIGHT, _TOP, _BOTTOM = 44, 8, 10, 26


@dataclass
class Series:
    label: str
    values: list[float]
    css: str  # "series-1".."series-5"


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1
    exponent = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * exponent:
            return step * exponent
    return 10 * exponent


def _plain(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def bar_chart(labels: list[str], series: list[Series], *, fmt: Callable[[float], str] = _plain,
              label_every: int | None = None, title: str = "") -> Markup:
    """Stacked bars, one per label (e.g. per day), with a y-axis and gridlines."""
    n = max(len(labels), 1)
    totals = [sum(s.values[i] for s in series) for i in range(len(labels))]
    top = _nice_max(max(totals, default=0))
    plot_w = WIDTH - _LEFT - _RIGHT
    plot_h = HEIGHT - _TOP - _BOTTOM
    slot = plot_w / n
    bar_w = max(1.0, slot * 0.72)
    label_every = label_every or max(1, math.ceil(n / 10))

    parts = [f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{escape(title)}">']
    for i in range(5):
        value = top * i / 4
        y = _TOP + plot_h - plot_h * i / 4
        parts.append(f'<line class="grid" x1="{_LEFT}" x2="{WIDTH - _RIGHT}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis" x="{_LEFT - 6}" y="{y + 4:.1f}" text-anchor="end">{escape(fmt(value))}</text>')
    for i, label in enumerate(labels):
        x = _LEFT + slot * i + (slot - bar_w) / 2
        base = _TOP + plot_h
        details = [f"{s.label}: {fmt(s.values[i])}" for s in series if s.values[i]]
        tip = escape(f"{label} -- " + (", ".join(details) if details else "none"))
        parts.append("<g>")
        parts.append(f"<title>{tip}</title>")
        # an invisible full-height hit area, so empty days still show a tooltip
        parts.append(f'<rect class="hit" x="{_LEFT + slot * i:.1f}" y="{_TOP}" width="{slot:.1f}" height="{plot_h}"/>')
        for s in series:
            h = plot_h * s.values[i] / top if top else 0
            if h <= 0:
                continue
            base -= h
            parts.append(f'<rect class="{s.css}" x="{x:.1f}" y="{base:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="1.5"/>')
        parts.append("</g>")
        if i % label_every == 0:
            parts.append(f'<text class="axis" x="{x + bar_w / 2:.1f}" y="{HEIGHT - 8}" text-anchor="middle">'
                         f"{escape(label)}</text>")
    parts.append("</svg>")
    legend = "".join(f'<span class="legend-item"><i class="swatch {s.css}"></i>{escape(s.label)}</span>' for s in series)
    return Markup(f'<div class="chart-wrap">{"".join(parts)}<div class="chart-legend">{legend}</div></div>')


def money(value: float) -> str:
    return f"${value:,.2f}" if value >= 1 or value == 0 else f"${value:,.4f}".rstrip("0")
