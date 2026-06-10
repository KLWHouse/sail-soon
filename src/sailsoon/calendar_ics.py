from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ics import Calendar, Event

from .config import Location
from .scoring import SailWindow


def _uid(kind: str, location_id: str, t: datetime, extra: str = "") -> str:
    """Stable UID so calendar clients update existing events instead of duplicating."""
    key = f"{kind}|{location_id}|{t.isoformat()}|{extra}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return f"{kind}-{location_id}-{digest}@sail-soon"


def _mean_wind_direction(degrees: Iterable[float | None]) -> float | None:
    vals = [d for d in degrees if d is not None]
    if not vals:
        return None
    sin_sum = sum(math.sin(math.radians(d)) for d in vals)
    cos_sum = sum(math.cos(math.radians(d)) for d in vals)
    if abs(sin_sum) < 1e-9 and abs(cos_sum) < 1e-9:
        return None
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360


def _compass_direction(degrees: float) -> str:
    sectors = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return sectors[int((degrees + 22.5) // 45) % len(sectors)]


def _window_event(loc: Location, w: SailWindow, verdict: str) -> Event:
    e = Event()
    flag = "GO" if verdict == "go" else "MAYBE"
    e.name = f"[{flag}] Sail @ {loc.name} ({w.avg_score:.2f})"
    e.begin = w.start
    e.end = w.end
    e.location = loc.name
    e.uid = _uid("sail", loc.id, w.start)

    hours = w.hours
    wind_vals = [h.raw.get("wind_speed_kt") for h in hours if h.raw.get("wind_speed_kt") is not None]
    wind_dir_vals = [h.raw.get("wind_dir_deg") for h in hours if h.raw.get("wind_dir_deg") is not None]
    gust_vals = [h.raw.get("wind_gust_kt") for h in hours if h.raw.get("wind_gust_kt") is not None]
    wave_vals = [h.raw.get("wave_height_m") for h in hours if h.raw.get("wave_height_m") is not None]
    temp_vals = [h.raw.get("air_temp_c") for h in hours if h.raw.get("air_temp_c") is not None]
    hazards = sorted({hz for h in hours for hz in h.hazards})

    lines = [
        f"Verdict: {verdict.upper()}",
        f"Avg score: {w.avg_score:.2f}  Min score: {w.min_score:.2f}",
        f"Duration: {w.duration_hours:.1f} h",
    ]
    if wind_vals:
        lines.append(f"Wind: {min(wind_vals):.0f}-{max(wind_vals):.0f} kt")
    wind_dir = _mean_wind_direction(wind_dir_vals)
    if wind_dir is not None:
        lines.append(f"Wind direction: {_compass_direction(wind_dir)} ({wind_dir:.0f} deg)")
    if gust_vals:
        lines.append(f"Gusts to: {max(gust_vals):.0f} kt")
    if wave_vals:
        lines.append(f"Waves to: {max(wave_vals):.1f} m")
    if temp_vals:
        low_c, high_c = min(temp_vals), max(temp_vals)
        low_f = low_c * 9 / 5 + 32
        high_f = high_c * 9 / 5 + 32
        lines.append(f"Air temp: {low_c:.0f}-{high_c:.0f} C ({low_f:.0f}-{high_f:.0f} F)")
    if hazards:
        lines.append("Hazards: " + ", ".join(hazards))
    e.description = "\n".join(lines)
    return e


def _tide_event(loc: Location, t: datetime, height_m: float, kind: str) -> Event:
    e = Event()
    label = "High" if kind == "H" else "Low"
    e.name = f"{label} tide @ {loc.name} ({height_m:.2f} m)"
    e.begin = t
    e.end = t + timedelta(minutes=15)
    e.location = loc.name
    e.uid = _uid("tide", loc.id, t, kind)
    e.description = f"{label} tide, {height_m:.2f} m MLLW (NOAA station {loc.tide_station})."
    return e


def build_calendar(
    *,
    locations_and_windows: Iterable[tuple[Location, SailWindow, str]] = (),
    tide_events: Iterable[tuple[Location, datetime, float, str]] = (),
    name: str = "Sail Soon",
) -> str:
    cal = Calendar()
    # ics 0.7 doesn't let us set X-WR-CALNAME directly, so clients will fall
    # back to the filename (/calendar.ics) for display.
    for loc, window, verdict in locations_and_windows:
        cal.events.add(_window_event(loc, window, verdict))
    for loc, t, h, kind in tide_events:
        cal.events.add(_tide_event(loc, t, h, kind))
    return cal.serialize()


def verdict_for_window(w: SailWindow) -> str:
    if w.avg_score >= 0.8 and w.duration_hours >= 4:
        return "go"
    return "maybe"
