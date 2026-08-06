"""SVG rendering. Pure: series in, SVG string out. No network, no clock, no files.

Output is deterministic. The hand-drawn wobble comes from a seeded generator
keyed on the data, not from randomness, so re-running on unchanged data produces
a byte-identical SVG. That matters because the intended use is committing the
chart to a repo, where nondeterministic output would mean a diff every run.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .github import Series

# Categorical hues validated for colour-vision deficiency in both light and dark
# modes; assigned by fixed slot order, never cycled or re-ranked.
LIGHT_SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
DARK_SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#008300", "#9085e9", "#e66767"]
MAX_SERIES = len(LIGHT_SERIES)

STAR_HISTORY_RED = "#dd4528"


@dataclass
class Theme:
    surface: str
    ink: str
    muted: str
    grid: str
    axis: str

    @staticmethod
    def of(dark: bool) -> "Theme":
        if dark:
            return Theme("#0d1117", "#e6edf3", "#8b949e", "#21262d", "#8b949e")
        return Theme("#ffffff", "#1f2328", "#656d76", "#eaeef2", "#1f2328")


@dataclass
class Options:
    width: int = 800
    height: int = 533
    title: str = "Star History"
    dark: bool = False
    xkcd: bool = True
    timeline: bool = False       # x = days since each repo's first star
    colors: list[str] | None = None
    attribution: str = ""


def render(series: list[Series], opts: Options) -> str:
    live = [s for s in series if s.points]
    if not live:
        raise ValueError("nothing to draw: every repo had zero stars")
    if len(live) > MAX_SERIES:
        raise ValueError(
            f"{len(live)} repos requested but the palette holds {MAX_SERIES}. "
            f"Beyond that, colours stop being distinguishable. Split the chart."
        )

    theme = Theme.of(opts.dark)
    palette = opts.colors or (DARK_SERIES if opts.dark else LIGHT_SERIES)
    if len(live) == 1 and not opts.colors:
        palette = [STAR_HISTORY_RED]

    pad = _padding(opts)
    plot_w = opts.width - pad["l"] - pad["r"]
    plot_h = opts.height - pad["t"] - pad["b"]

    tracks = [_track(s, opts.timeline) for s in live]
    x_max = max(t[-1][0] for t in tracks)
    y_max, y_ticks = _nice_scale(max(max(y for _, y in t) for t in tracks))
    # Timeline mode starts every repo at day 0; date mode spans the real range.
    x_min = 0.0 if opts.timeline else min(t[0][0] for t in tracks)

    def sx(v: float) -> float:
        span = (x_max - x_min) or 1.0
        return pad["l"] + (v - x_min) / span * plot_w

    def sy(v: float) -> float:
        return pad["t"] + plot_h - (v / y_max) * plot_h

    jitter = _Wobble(seed=_seed(live), on=opts.xkcd)
    out: list[str] = []
    out.append(_open(opts, theme))
    out.append(_defs(opts))
    out.append(f'<rect width="{opts.width}" height="{opts.height}" fill="{theme.surface}"/>')
    out.append(_title(opts, theme))
    out.append(_grid(opts, theme, pad, plot_w, y_ticks, sy, jitter))
    out.append(_x_axis(opts, theme, pad, plot_h, x_min, x_max, sx, live, jitter))
    out.append(_y_label(opts, theme, pad, plot_h))

    for index, (s, track) in enumerate(zip(live, tracks)):
        color = palette[index % len(palette)]
        out.append(_series(track, color, sx, sy, pad, plot_h, jitter,
                           fill=len(live) == 1))

    out.append(_legend(live, palette, opts, theme, pad))
    out.append(_end_values(live, tracks, palette, sx, sy, opts, theme))
    if opts.attribution:
        out.append(_attribution(opts, theme))
    out.append("</svg>")
    return "\n".join(part for part in out if part)


# --- geometry ---------------------------------------------------------------

def _track(s: Series, timeline: bool) -> list[tuple[float, float]]:
    """Series -> (x, y) pairs. x is a POSIX timestamp, or days since first star."""
    origin = s.points[0][0]
    if timeline:
        return [((t - origin).total_seconds() / 86400.0, float(c)) for t, c in s.points]
    return [(t.timestamp(), float(c)) for t, c in s.points]


def _padding(opts: Options) -> dict[str, int]:
    return {"l": 78, "r": 46, "t": 62, "b": 78 if opts.attribution else 62}


def _nice_scale(value: float, count: int = 4) -> tuple[float, list[float]]:
    """Pick a top-of-axis and ticks that land on round numbers.

    Scaling the max alone is not enough: dividing 75 into 4 gives 18.75, and an
    axis labelled 18.8 / 37.5 / 56.2 is unreadable. So choose a round STEP first
    and let the top follow from it.
    """
    if value <= count:
        return float(count), [float(i) for i in range(count + 1)]
    raw = value / count
    magnitude = 10.0 ** math.floor(math.log10(raw))
    step = next((m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw),
                10 * magnitude)
    return step * count, [step * i for i in range(count + 1)]


class _Wobble:
    """Deterministic hand-drawn displacement. Seeded, so output never churns."""

    def __init__(self, seed: int, on: bool, amount: float = 1.15) -> None:
        self._state = seed or 1
        self._on = on
        self._amount = amount

    def __call__(self) -> float:
        if not self._on:
            return 0.0
        # Numerical Recipes LCG: cheap, stdlib-free, entirely reproducible.
        self._state = (1664525 * self._state + 1013904223) % (2 ** 32)
        return ((self._state / 2 ** 32) - 0.5) * 2 * self._amount

    def path(self, points: list[tuple[float, float]]) -> str:
        parts = []
        for i, (x, y) in enumerate(points):
            parts.append(f"{'M' if i == 0 else 'L'}{x + self():.2f} {y + self():.2f}")
        return "".join(parts)


def _seed(series: list[Series]) -> int:
    raw = "|".join(f"{s.repo}:{s.total}:{len(s.points)}" for s in series)
    return sum((i + 1) * ord(ch) for i, ch in enumerate(raw)) % (2 ** 31)


# --- svg parts --------------------------------------------------------------

def _font(opts: Options) -> str:
    return "Handlee, cursive" if opts.xkcd else \
        "system-ui, -apple-system, 'Segoe UI', sans-serif"


def _open(opts: Options, theme: Theme) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{opts.width}" '
        f'height="{opts.height}" viewBox="0 0 {opts.width} {opts.height}" '
        f'font-family="{_font(opts)}">'
    )


def _defs(opts: Options) -> str:
    if not opts.xkcd:
        return ""
    font = Path(__file__).parent / "assets" / "handlee.woff2"
    try:
        encoded = base64.b64encode(font.read_bytes()).decode("ascii")
    except OSError:
        return ""
    # Embedded so the chart renders identically everywhere with no network call.
    return (
        "<defs><style>@font-face{font-family:'Handlee';font-style:normal;"
        "font-weight:400;src:url(data:font/woff2;base64,"
        f"{encoded}) format('woff2');}}</style></defs>"
    )


def _title(opts: Options, theme: Theme) -> str:
    return (
        f'<text x="{opts.width / 2:.0f}" y="34" text-anchor="middle" '
        f'font-size="21" fill="{theme.ink}">{_esc(opts.title)}</text>'
    )


def _grid(opts, theme, pad, plot_w, y_ticks, sy, jitter) -> str:
    parts = []
    for tick in y_ticks:
        y = sy(tick)
        parts.append(
            f'<line x1="{pad["l"]}" x2="{pad["l"] + plot_w}" y1="{y:.1f}" '
            f'y2="{y + jitter() * 0.4:.1f}" stroke="{theme.grid}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad["l"] - 12}" y="{y + 5:.1f}" text-anchor="end" '
            f'font-size="15" fill="{theme.muted}">{_num(tick)}</text>'
        )
    return "".join(parts)


def _x_axis(opts, theme, pad, plot_h, x_min, x_max, sx, live, jitter) -> str:
    base = pad["t"] + plot_h
    parts = [
        f'<path d="{jitter.path([(pad["l"], base), (opts.width - pad["r"], base)])}" '
        f'stroke="{theme.axis}" stroke-width="2" fill="none" stroke-linecap="round"/>',
        f'<path d="{jitter.path([(pad["l"], pad["t"] - 8), (pad["l"], base)])}" '
        f'stroke="{theme.axis}" stroke-width="2" fill="none" stroke-linecap="round"/>',
    ]
    for value, label in _x_ticks(x_min, x_max, opts.timeline):
        x = sx(value)
        anchor = "start" if value == x_min else ("end" if value == x_max else "middle")
        parts.append(
            f'<text x="{x:.1f}" y="{base + 26:.0f}" text-anchor="{anchor}" '
            f'font-size="15" fill="{theme.muted}">{_esc(label)}</text>'
        )
    caption = "Days since first star" if opts.timeline else "Date"
    parts.append(
        f'<text x="{opts.width / 2:.0f}" y="{base + 52:.0f}" text-anchor="middle" '
        f'font-size="16" fill="{theme.muted}">{caption}</text>'
    )
    return "".join(parts)


def _x_ticks(x_min: float, x_max: float, timeline: bool) -> list[tuple[float, str]]:
    steps = 4
    values = [x_min + (x_max - x_min) * i / steps for i in range(steps + 1)]
    if timeline:
        return [(v, f"{round(v)}d") for v in values]
    span_days = (x_max - x_min) / 86400.0
    fmt = "%b %Y" if span_days > 200 else "%b %d"
    return [(v, datetime.fromtimestamp(v).strftime(fmt)) for v in values]


def _y_label(opts, theme, pad, plot_h) -> str:
    y = pad["t"] + plot_h / 2
    return (
        f'<text x="22" y="{y:.0f}" text-anchor="middle" font-size="16" '
        f'fill="{theme.muted}" transform="rotate(-90 22 {y:.0f})">GitHub Stars</text>'
    )


def _series(track, color, sx, sy, pad, plot_h, jitter, fill: bool) -> str:
    points = [(sx(x), sy(y)) for x, y in track]
    line = jitter.path(points)
    parts = []
    if fill:
        base = pad["t"] + plot_h
        area = f"{line}L{points[-1][0]:.2f} {base:.2f}L{points[0][0]:.2f} {base:.2f}Z"
        parts.append(f'<path d="{area}" fill="{color}" opacity="0.12"/>')
    parts.append(
        f'<path d="{line}" fill="none" stroke="{color}" stroke-width="2.6" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
    )
    parts.append(
        f'<circle cx="{points[-1][0]:.2f}" cy="{points[-1][1]:.2f}" r="4.5" fill="{color}"/>'
    )
    return "".join(parts)


def _legend(live, palette, opts, theme, pad) -> str:
    # Carry the current count in the legend rather than floating it beside the
    # line. With several flat series the end labels have to be pushed apart to
    # avoid overlapping, which detaches them from the curve they describe and
    # makes the chart read wrong. The legend has room and needs no dodging.
    rows = [f"{s.repo}  {s.total}" for s in live] if len(live) > 1 else [live[0].repo]
    width = 26 + int(max(len(r) for r in rows) * 8.4)
    height = 12 + 23 * len(rows)
    x, y = pad["l"] + 18, pad["t"] + 10
    parts = [
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="7" '
        f'fill="{theme.surface}" stroke="{theme.axis}" stroke-width="1.6" opacity="0.96"/>'
    ]
    for i, label in enumerate(rows):
        cy = y + 20 + i * 23
        parts.append(
            f'<rect x="{x + 11}" y="{cy - 8}" width="10" height="10" rx="2" '
            f'fill="{palette[i % len(palette)]}"/>'
        )
        parts.append(
            f'<text x="{x + 28}" y="{cy + 1}" font-size="15" fill="{theme.ink}">'
            f"{_esc(label)}</text>"
        )
    return "".join(parts)


def _end_values(live, tracks, palette, sx, sy, opts, theme) -> str:
    """Label the final point, but only for a single series.

    With more than one, the counts live in the legend (see _legend) because
    dodged labels stop pointing at their own curve.
    """
    if len(live) != 1:
        return ""
    x, y = sx(tracks[0][-1][0]), sy(tracks[0][-1][1])
    return (
        f'<text x="{min(x + 8, opts.width - 6):.1f}" y="{y - 12:.1f}" '
        f'text-anchor="end" font-size="17" fill="{palette[0]}">{live[0].total}</text>'
    )


def _attribution(opts: Options, theme: Theme) -> str:
    return (
        f'<text x="{opts.width - 14}" y="{opts.height - 12}" text-anchor="end" '
        f'font-size="12" fill="{theme.muted}">{_esc(opts.attribution)}</text>'
    )


def _num(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
