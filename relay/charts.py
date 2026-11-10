"""Inline SVG charts for the dashboard: no JavaScript, colours from CSS classes (`s1`..`s4`)."""

from __future__ import annotations

from html import escape

from markupsafe import Markup

WIDTH, HEIGHT = 720, 190
LEFT, RIGHT, TOP, BOTTOM = 56, 8, 10, 24


def _scale(series: list[list[float | None]]) -> float:
    peak = max((v for values in series for v in values if v is not None), default=0)
    if peak <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(peak))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        if peak <= step * magnitude:
            return step * magnitude
    return peak


def _axis(value: float, top: float, unit: str) -> str:
    """Short tick labels: 12k instead of 12,000; milliseconds from 1,000 up as seconds."""
    if unit.strip() == "ms" and top >= 1000:
        return f"{value / 1000:,.0f} s" if top >= 10_000 else f"{value / 1000:,.1f} s"
    if top >= 10_000:
        return f"{value / 1000:,.0f}k{unit}"
    return f"{value:,.0f}{unit}" if top >= 10 else f"{value:,.1f}{unit}"


def _frame(labels: list[str], top: float, unit: str) -> list[str]:
    inner_w, inner_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    parts = []
    for fraction in (0, 0.5, 1):
        y = TOP + inner_h * (1 - fraction)
        text = _axis(top * fraction, top, unit)
        parts.append(f'<line class="gridline" x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis" x="{LEFT - 6}" y="{y + 4:.1f}" text-anchor="end">{escape(text)}</text>')
    step = max(1, len(labels) // 7)
    slot = inner_w / max(1, len(labels))
    for i, label in enumerate(labels):
        if i % step == 0 or i == len(labels) - 1:
            x = LEFT + slot * (i + 0.5)
            parts.append(
                f'<text class="axis" x="{x:.1f}" y="{HEIGHT - 6}" text-anchor="middle">{escape(label[5:])}</text>'
            )
    return parts


def bars(labels: list[str], series: list[list[float | None]], unit: str = "") -> Markup:
    """Grouped bars: one group per label, one bar per series."""
    top = _scale(series)
    inner_w, inner_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    slot = inner_w / max(1, len(labels))
    bar_w = slot * 0.8 / max(1, len(series))
    parts = _frame(labels, top, unit)
    for i in range(len(labels)):
        for s, values in enumerate(series):
            value = values[i]
            if not value:
                continue
            h = inner_h * value / top
            x = LEFT + slot * i + slot * 0.1 + bar_w * s
            parts.append(
                f'<rect class="s{s + 1}" x="{x:.1f}" y="{TOP + inner_h - h:.1f}" width="{bar_w - 1:.1f}"'
                f' height="{h:.1f}" rx="2"><title>{escape(labels[i])}: {value:,.4g}{escape(unit)}</title></rect>'
            )
    return _svg(parts)


def lines(labels: list[str], series: list[list[float | None]], unit: str = "", top: float | None = None) -> Markup:
    """One line per series; gaps where a value is None."""
    top = top or _scale(series)
    inner_w, inner_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    slot = inner_w / max(1, len(labels))
    parts = _frame(labels, top, unit)
    for s, values in enumerate(series):
        segment: list[str] = []
        segments = []
        for i, value in enumerate(values):
            if value is None:
                if segment:
                    segments.append(segment)
                segment = []
                continue
            x, y = LEFT + slot * (i + 0.5), TOP + inner_h * (1 - min(value, top) / top)
            segment.append(f"{x:.1f},{y:.1f}")
            parts.append(
                f'<circle class="s{s + 1}" cx="{x:.1f}" cy="{y:.1f}" r="2.5">'
                f"<title>{escape(labels[i])}: {value:,.4g}{escape(unit)}</title></circle>"
            )
        if segment:
            segments.append(segment)
        for points in segments:
            parts.append(f'<polyline class="line s{s + 1}" points="{" ".join(points)}"/>')
    return _svg(parts)


def _svg(parts: list[str]) -> Markup:
    body = "".join(parts)
    return Markup(f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img">{body}</svg>')
