from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Location as LocationCfg
from .config import get_location, load_locations, load_rules
from .db import get_sessionmaker
from .models import MarineForecast, TidePrediction
from .scoring import (
    HourReport,
    SailWindow,
    day_range,
    find_windows,
    score_range,
)


app = FastAPI(
    title="sail-soon",
    version="0.1.0",
    description=(
        "Aggregated NOAA + Open-Meteo marine data for Long Island Sound with "
        "configurable sail-condition scoring. Designed to be called by LLM agents: "
        "hit /sail-windows to answer 'when should I sail this week?' or "
        "/summary/{when} to answer 'is tomorrow any good?'."
    ),
)


def get_db():
    sm = get_sessionmaker()
    session = sm()
    try:
        yield session
    finally:
        session.close()


# ---------- schemas ----------


class LocationOut(BaseModel):
    id: str
    name: str
    tide_station: str
    marine_zone: str
    latitude: float
    longitude: float
    timezone: str

    @classmethod
    def from_cfg(cls, loc: LocationCfg) -> "LocationOut":
        return cls(**loc.model_dump())


class HourOut(BaseModel):
    t: datetime
    score: float
    label: str
    daylight: bool
    reasons: dict[str, float]
    hazards: list[str]
    wind_speed_kt: float | None
    wind_gust_kt: float | None
    wind_dir_deg: float | None
    wave_height_m: float | None
    precip_prob_pct: float | None
    air_temp_c: float | None
    cloud_cover_pct: float | None

    @classmethod
    def from_report(cls, r: HourReport) -> "HourOut":
        return cls(
            t=r.t,
            score=r.score,
            label=r.label,
            daylight=r.daylight,
            reasons=r.reasons,
            hazards=r.hazards,
            **{k: r.raw.get(k) for k in (
                "wind_speed_kt", "wind_gust_kt", "wind_dir_deg",
                "wave_height_m", "precip_prob_pct", "air_temp_c", "cloud_cover_pct",
            )},
        )


class WindowOut(BaseModel):
    start: datetime
    end: datetime
    duration_hours: float
    avg_score: float
    min_score: float
    summary: str
    hours: list[HourOut]

    @classmethod
    def from_window(cls, w: SailWindow) -> "WindowOut":
        return cls(
            start=w.start,
            end=w.end,
            duration_hours=round(w.duration_hours, 2),
            avg_score=w.avg_score,
            min_score=w.min_score,
            summary=_window_summary(w),
            hours=[HourOut.from_report(h) for h in w.hours],
        )


class DaySummary(BaseModel):
    location_id: str
    date: date
    verdict: Literal["go", "maybe", "no-go", "no-data"]
    best_window: WindowOut | None
    reason: str
    windows: list[WindowOut]


class TideOut(BaseModel):
    t: datetime
    height_m: float
    type: str | None


# ---------- helpers ----------


def _window_summary(w: SailWindow) -> str:
    # Compact English description so agents don't have to re-reason from raw.
    hours = w.hours
    if not hours:
        return "No data."
    wind_vals = [h.raw.get("wind_speed_kt") for h in hours if h.raw.get("wind_speed_kt") is not None]
    gust_vals = [h.raw.get("wind_gust_kt") for h in hours if h.raw.get("wind_gust_kt") is not None]
    wave_vals = [h.raw.get("wave_height_m") for h in hours if h.raw.get("wave_height_m") is not None]
    parts = [f"{round(w.duration_hours)}h window", f"score {w.avg_score:.2f}"]
    if wind_vals:
        parts.append(f"wind {min(wind_vals):.0f}-{max(wind_vals):.0f} kt")
    if gust_vals:
        parts.append(f"gusts to {max(gust_vals):.0f} kt")
    if wave_vals:
        parts.append(f"waves to {max(wave_vals):.1f} m")
    return ", ".join(parts) + "."


def _resolve_when(when: str, loc: LocationCfg) -> tuple[datetime, datetime, str]:
    """Return (start_utc, end_utc, label)."""
    today_local = datetime.now(tz=timezone.utc).astimezone().date()
    w = when.lower()
    if w == "today":
        s, e = day_range(loc, today_local)
        return s, e, "today"
    if w == "tomorrow":
        s, e = day_range(loc, today_local + timedelta(days=1))
        return s, e, "tomorrow"
    if w in ("week", "this-week", "this_week"):
        s, _ = day_range(loc, today_local)
        _, e = day_range(loc, today_local + timedelta(days=6))
        return s, e, "this-week"
    # Try ISO date.
    try:
        d = date.fromisoformat(when)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=f"Bad `when`: {when}") from err
    s, e = day_range(loc, d)
    return s, e, d.isoformat()


def _verdict(windows: list[SailWindow]) -> tuple[Literal["go", "maybe", "no-go", "no-data"], str]:
    if not windows:
        return "no-go", "No daylight hours meet the score threshold."
    best = max(windows, key=lambda w: w.avg_score)
    if best.avg_score >= 0.8 and best.duration_hours >= 4:
        return "go", f"Solid window: {_window_summary(best)}"
    if best.avg_score >= 0.65:
        return "maybe", f"Marginal window: {_window_summary(best)}"
    return "no-go", f"Best available was weak: {_window_summary(best)}"


# ---------- endpoints ----------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/locations", response_model=list[LocationOut])
def locations() -> list[LocationOut]:
    return [LocationOut.from_cfg(l) for l in load_locations()]


@app.get("/conditions", response_model=list[HourOut])
def conditions(
    location: str = Query(..., description="Location id, e.g. 'kings_point'."),
    when: str = Query("today", description="today | tomorrow | this-week | YYYY-MM-DD"),
    db: Session = Depends(get_db),
) -> list[HourOut]:
    loc = get_location(location)
    start, end, _ = _resolve_when(when, loc)
    reports = score_range(db, loc, start, end)
    return [HourOut.from_report(r) for r in reports]


@app.get("/sail-windows", response_model=list[WindowOut])
def sail_windows(
    location: str = Query(..., description="Location id."),
    when: str = Query("this-week"),
    db: Session = Depends(get_db),
) -> list[WindowOut]:
    """Return every contiguous span of daylight hours meeting the score threshold."""
    loc = get_location(location)
    rules = load_rules()
    start, end, _ = _resolve_when(when, loc)
    reports = score_range(db, loc, start, end, rules)
    windows = find_windows(reports, rules)
    windows.sort(key=lambda w: w.avg_score, reverse=True)
    return [WindowOut.from_window(w) for w in windows]


@app.get("/summary/{when}", response_model=DaySummary)
def summary(
    when: str,
    location: str = Query(..., description="Location id."),
    db: Session = Depends(get_db),
) -> DaySummary:
    """High-level verdict for a day. Ideal for an agent answering 'is X good?'."""
    loc = get_location(location)
    rules = load_rules()
    start, end, label = _resolve_when(when, loc)
    reports = score_range(db, loc, start, end, rules)
    windows = find_windows(reports, rules)
    windows_sorted = sorted(windows, key=lambda w: w.avg_score, reverse=True)
    verdict, reason = _verdict(windows_sorted)
    return DaySummary(
        location_id=loc.id,
        date=start.astimezone().date(),
        verdict=verdict,
        best_window=WindowOut.from_window(windows_sorted[0]) if windows_sorted else None,
        reason=reason,
        windows=[WindowOut.from_window(w) for w in windows_sorted],
    )


@app.get("/tides", response_model=list[TideOut])
def tides(
    location: str = Query(...),
    when: str = Query("this-week"),
    extrema_only: bool = Query(True, description="Only high/low, not hourly."),
    db: Session = Depends(get_db),
) -> list[TideOut]:
    loc = get_location(location)
    start, end, _ = _resolve_when(when, loc)
    q = (
        select(TidePrediction)
        .where(TidePrediction.location_id == loc.id)
        .where(TidePrediction.t >= start)
        .where(TidePrediction.t < end)
        .order_by(TidePrediction.t)
    )
    if extrema_only:
        q = q.where(TidePrediction.type.isnot(None))
    rows = db.execute(q).scalars().all()
    return [TideOut(t=r.t, height_m=r.height_m, type=r.type) for r in rows]


@app.get("/marine", response_model=list[dict])
def marine(
    location: str = Query(...),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    loc = get_location(location)
    rows = (
        db.execute(
            select(MarineForecast)
            .where(MarineForecast.location_id == loc.id)
            .order_by(MarineForecast.valid_from)
        )
        .scalars()
        .all()
    )
    return [
        {
            "zone": r.zone,
            "valid_from": r.valid_from,
            "valid_to": r.valid_to,
            "headline": r.headline,
            "hazards": r.hazards,
            "text": r.raw_text,
        }
        for r in rows
    ]
