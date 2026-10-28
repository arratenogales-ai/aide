"""Whether it will rain today, for the morning summary. By code, with Open-Meteo and
without a model.

The places (and from which day each is valid, for example after a move) and the
thresholds are in a JSON file (`morning_summary.max_wait` in settings.yaml, by default
`agent/projects/rain-alert/config.json`), which is not in git because it says where
the user lives. Without it there is no weather line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
SECONDS = 15
KEYS = (
    "locations",
    "time_zone",
    "start_hour",
    "end_hour",
    "probability_threshold",
    "mm_threshold",
)


@dataclass
class Weather:
    """The rain phrase (None if it does not rain) and whether it could not be determined."""

    phrase: str | None = None
    failure: bool = False


def _valid_site(u: Any) -> bool:
    try:
        return (
            isinstance(u, dict)
            and -90 <= float(u["latitude"]) <= 90
            and -180 <= float(u["longitude"]) <= 180
            and date.fromisoformat(str(u["since"])) is not None
        )
    except (KeyError, TypeError, ValueError):
        return False


def load(path: Path) -> dict[str, Any] | None:
    """The rain alert configuration, or None if it is missing or invalid."""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or any(k not in cfg for k in KEYS):
        return None
    sites = cfg["locations"]
    if not isinstance(sites, list) or not sites or not all(_valid_site(u) for u in sites):
        return None
    return cfg


def site(cfg: dict[str, Any], day: date) -> dict[str, Any] | None:
    """The last place whose “since” is `day` or earlier."""
    try:
        current = [u for u in cfg["locations"] if date.fromisoformat(u["since"]) <= day]
    except (KeyError, TypeError, ValueError):
        return None
    return max(current, key=lambda u: u["since"]) if current else None


def _number(value: Any) -> float:
    return 0.0 if value is None else float(value)


def analyze(reply: dict[str, Any], day: date, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The rain window of `day` between the hours in the configuration, or None if it does not
    rain. Raises ValueError if the response is not valid."""
    try:
        h = reply["hourly"]
        rows = list(zip(h["time"], h["precipitation_probability"], h["precipitation"], strict=True))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"invalid Open-Meteo response ({type(e).__name__})") from e
    hours = []
    for t, p, mm in rows:
        moment = datetime.fromisoformat(t)
        if moment.date() == day and cfg["start_hour"] <= moment.hour <= cfg["end_hour"]:
            hours.append((moment.hour, _number(p), _number(mm)))
    if not hours:
        raise ValueError("the response doesn't include today's hours")
    rainy = [
        x for x in hours if x[1] >= cfg["probability_threshold"] or x[2] >= cfg["mm_threshold"]
    ]
    if not rainy:
        return None
    first, last = rainy[0][0], rainy[-1][0]
    band = [x for x in hours if first <= x[0] <= last]
    return {
        "since": first,
        "until": last,
        "prob_max": max(x[1] for x in band),
        "mm_total": round(sum(x[2] for x in band), 1),
    }


def phrase(name: str, r: dict[str, Any]) -> str:
    """Builds the rain alert message for a place and a time range."""
    if r["since"] == r["until"]:
        timing = f"at {r['since']:02d}:00"
    else:
        timing = f"between {r['since']:02d}:00 and {r['until']:02d}:00"
    detail = f"with a chance of up to {round(r['prob_max'])}%"
    if r["mm_total"] > 0:
        detail += f" and about {r['mm_total']:.1f} mm in total"
    return f"☔ It's going to rain in {name} today {timing}, {detail}. Take an umbrella."


def _request(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "aide/1.0"})
    with urllib.request.urlopen(request, timeout=SECONDS) as r:
        return json.loads(r.read().decode("utf-8"))


async def rain(cfg_path: Path, day: date) -> Weather:
    """Whether it will rain on `day` where the user lives that day. Without their
    configuration, or if it is invalid or has no place for that day, it says it could not be
    checked (as the rain alert did), and it is left in the log, without its contents."""
    cfg = load(cfg_path)
    if cfg is None:
        log.warning("the weather settings are missing or invalid (%s)", cfg_path.name)
        return Weather(failure=True)
    place = site(cfg, day)
    if place is None:
        log.warning("the weather settings have no place for today")
        return Weather(failure=True)
    parameters = {
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "hourly": "precipitation_probability,precipitation",
        "timezone": cfg["time_zone"],
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
    }
    url = URL + "?" + urllib.parse.urlencode(parameters, safe=",")
    for attempt in range(2):
        try:
            reply = await asyncio.to_thread(_request, url)
            r = analyze(reply, day, cfg)
        except Exception as e:  # network, JSON or odd response: one retry and then it is reported
            log.info("could not check the weather (%s)", type(e).__name__)
            if attempt == 0:
                await asyncio.sleep(2)
            continue
        return Weather(phrase(str(place.get("name") or "your area"), r) if r else None)
    return Weather(failure=True)
