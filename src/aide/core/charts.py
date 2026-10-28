"""PNG charts for deliveries with figures.

The code generates them with matplotlib (headless) from some figures: the agent requests them with
`generate_chart` when a delivery compares figures (trends, sales) and the system makes them on its
own, without a model, for usage (/uso and the Sunday report). Any PNG in results/ reaches the user
as an image in Telegram.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KINDS = ("bars", "horizontal_bars", "lines")
# Pastel colors, like the panel.
PALETTE = ["#6C9BD2", "#F2A97E", "#86C79C", "#B79AD9", "#E8C25A", "#E591A8", "#7CC4C4", "#A7A7A7"]
MAX_LABELS = 60
MAX_SERIES = 8


class ChartError(ValueError):
    """The data is not suitable for a chart."""


@dataclass
class Series:
    """A named list of numeric values for a chart."""

    name: str
    values: list[float]


def is_number(value: float, decimals: int = 0) -> str:
    """1234567.8 -> “1,234,568” (or with decimals: “1,234.5”), as written in English. The name is
    the usual one; the format is no longer Spanish."""
    return f"{value:,.{decimals}f}"


def _abbreviated(value: float, _pos: Any = None) -> str:
    """For the axes: 1,500,000 -> “1.5M”; 12,000 -> “12K”."""
    absolute = abs(value)
    if absolute >= 1_000_000:
        return is_number(value / 1_000_000, 1).removesuffix(".0") + "M"
    if absolute >= 10_000:
        return is_number(value / 1_000, 0) + "K"
    if absolute >= 1 or value == 0:
        return is_number(value, 0 if float(value).is_integer() else 1)
    return is_number(value, 2)


def validate(kind: str, labels: list[Any], series: list[Any]) -> tuple[list[str], list[Series]]:
    """Validates chart kind, labels and series, raising ChartError if invalid."""
    if kind not in KINDS:
        raise ChartError(f"invalid kind: {kind} (use {', '.join(KINDS)})")
    if not labels or len(labels) > MAX_LABELS:
        raise ChartError(f"between 1 and {MAX_LABELS} labels are needed")
    if not series or len(series) > MAX_SERIES:
        raise ChartError(f"between 1 and {MAX_SERIES} series are needed")
    output = []
    for s in series:
        if not isinstance(s, dict):
            raise ChartError("each series is an object with name and values")
        values = s.get("values")
        if not isinstance(values, list) or len(values) != len(labels):
            raise ChartError(
                f'series "{s.get("name", "")}" must have one value per label ({len(labels)})'
            )
        try:
            numbers = [float(v) for v in values]
        except (TypeError, ValueError) as e:
            raise ChartError(f'series "{s.get("name", "")}" has values that are not numbers') from e
        if any(math.isnan(v) or math.isinf(v) for v in numbers):
            raise ChartError("some values are not finite numbers")
        output.append(Series(str(s.get("name") or "").strip()[:60], numbers))
    return [str(e)[:40] for e in labels], output


def generate(
    output: Path,
    kind: str,
    title: str,
    labels: list[Any],
    series: list[dict[str, Any]],
    *,
    axis_and: str = "",
    stacked: bool = False,
    note: str = "",
) -> Path:
    """Draws the chart and saves it as PNG. Raises ChartError if the data is not valid."""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.ticker import FuncFormatter

    names, data = validate(kind, labels, series)
    output.parent.mkdir(parents=True, exist_ok=True)
    height = max(4.8, 0.32 * len(names) + 1.6) if kind == "horizontal_bars" else 5.4
    fig, ax = plt.subplots(figsize=(10, height), dpi=150)
    try:
        fig.patch.set_facecolor("white")
        positions = list(range(len(names)))
        if kind == "lines":
            for i, s in enumerate(data):
                ax.plot(
                    positions,
                    s.values,
                    marker="o",
                    linewidth=2,
                    color=PALETTE[i % len(PALETTE)],
                    label=s.name or None,
                )
            ax.set_xticks(
                positions,
                names,
                rotation=45 if len(names) > 8 else 0,
                ha="right" if len(names) > 8 else "center",
            )
        else:
            horizontal = kind == "horizontal_bars"
            group_width = 0.8
            width = group_width if stacked else group_width / len(data)
            base = [0.0] * len(names)
            for i, s in enumerate(data):
                scroll = 0.0 if stacked else (i - (len(data) - 1) / 2) * width
                pos = [p + scroll for p in positions]
                color = PALETTE[i % len(PALETTE)]
                if horizontal:
                    ax.barh(
                        pos,
                        s.values,
                        height=width,
                        left=base if stacked else None,
                        color=color,
                        label=s.name or None,
                    )
                else:
                    ax.bar(
                        pos,
                        s.values,
                        width=width,
                        bottom=base if stacked else None,
                        color=color,
                        label=s.name or None,
                    )
                if stacked:
                    base = [b + v for b, v in zip(base, s.values, strict=True)]
            if horizontal:
                ax.set_yticks(positions, names)
                ax.invert_yaxis()
            else:
                ax.set_xticks(
                    positions,
                    names,
                    rotation=45 if len(names) > 8 else 0,
                    ha="right" if len(names) > 8 else "center",
                )
        axis = ax.xaxis if kind == "horizontal_bars" else ax.yaxis
        axis.set_major_formatter(FuncFormatter(_abbreviated))
        if axis_and:
            (ax.set_xlabel if kind == "horizontal_bars" else ax.set_ylabel)(axis_and)
        ax.set_title(title[:120], fontsize=14, pad=12)
        ax.grid(axis="x" if kind == "horizontal_bars" else "y", color="#E5E5E5", linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        if any(s.name for s in data) and len(data) > 1:
            ax.legend(frameon=False)
        if note:
            fig.text(0.01, 0.01, note[:160], fontsize=8, color="#777777")
        fig.tight_layout()
        fig.savefig(output, format="png")
    finally:
        plt.close(fig)
    return output


WEEK_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


async def consumption_chart(core: Any, days: int, moment: Any = None) -> Path | None:
    """Chart of the agent's usage by day and model (without a model, for /uso and the Sunday
    report). It goes to data/charts/; None if there is no usage on those days."""
    import asyncio
    from datetime import date

    from aide.core.usage import format_tokens

    zone = core.config.time_zone
    by_day = await core.usage.consumption_by_day(days, zone, moment)
    total = sum(sum(v.values()) for _, v in by_day)
    if not total:
        return None
    families = sorted(
        {f for _, v in by_day for f in v},
        key=lambda f: (
            ("Fable", "Opus", "Sonnet", "Haiku").index(f)
            if f in ("Fable", "Opus", "Sonnet", "Haiku")
            else 9
        ),
    )
    labels = []
    for day, _ in by_day:
        d = date.fromisoformat(day)
        labels.append(f"{WEEK_DAYS[d.weekday()]} {d.day}")
    series = [{"name": f, "values": [v.get(f, 0) for _, v in by_day]} for f in families]
    destination = core.paths.data / "charts" / f"usage-{days}-days-{by_day[-1][0]}.png"
    await asyncio.to_thread(
        generate,
        destination,
        "bars",
        f"Agent usage, last {days} days ({format_tokens(total)})",
        labels,
        series,
        axis_and="tokens",
        stacked=True,
        note="The agent's tokens by day and model. It doesn't include what you use in Claude Code or claude.ai.",
    )
    return destination
