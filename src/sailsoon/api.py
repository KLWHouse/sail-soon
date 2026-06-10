from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .calendar_ics import build_calendar, verdict_for_window
from .config import Location as LocationCfg
from .config import RuleSet, get_location, load_locations, load_rules
from .db import get_sessionmaker
from .models import HourlyForecast, MarineForecast, TidePrediction
from .profiles import (
    ProfileConfig,
    ProfileCreated,
    ProfileIn,
    ProfileOut,
    ProfileUpdate,
    create_profile,
    delete_profile,
    effective_ruleset,
    get_profile,
    list_profiles,
    update_profile,
)
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


class LocationIngestStatus(BaseModel):
    location_id: str
    latest_hourly_time: datetime | None
    latest_hourly_fetched_at: datetime | None
    hourly_rows: int
    latest_tide_time: datetime | None
    tide_rows: int
    latest_marine_valid_to: datetime | None
    latest_marine_fetched_at: datetime | None
    marine_rows: int


class IngestStatus(BaseModel):
    generated_at: datetime
    locations: list[LocationIngestStatus]


# ---------- helpers ----------


def _mean_wind_direction(degrees: list[float | None]) -> float | None:
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


def _window_summary(w: SailWindow) -> str:
    # Compact English description so agents don't have to re-reason from raw.
    hours = w.hours
    if not hours:
        return "No data."
    wind_vals = [h.raw.get("wind_speed_kt") for h in hours if h.raw.get("wind_speed_kt") is not None]
    wind_dir_vals = [h.raw.get("wind_dir_deg") for h in hours if h.raw.get("wind_dir_deg") is not None]
    gust_vals = [h.raw.get("wind_gust_kt") for h in hours if h.raw.get("wind_gust_kt") is not None]
    wave_vals = [h.raw.get("wave_height_m") for h in hours if h.raw.get("wave_height_m") is not None]
    temp_vals = [h.raw.get("air_temp_c") for h in hours if h.raw.get("air_temp_c") is not None]
    parts = [f"{round(w.duration_hours)}h window", f"score {w.avg_score:.2f}"]
    if wind_vals:
        parts.append(f"wind {min(wind_vals):.0f}-{max(wind_vals):.0f} kt")
    wind_dir = _mean_wind_direction(wind_dir_vals)
    if wind_dir is not None:
        parts.append(f"from {_compass_direction(wind_dir)} ({wind_dir:.0f} deg)")
    if gust_vals:
        parts.append(f"gusts to {max(gust_vals):.0f} kt")
    if wave_vals:
        parts.append(f"waves to {max(wave_vals):.1f} m")
    if temp_vals:
        parts.append(f"air {min(temp_vals):.0f}-{max(temp_vals):.0f} C")
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


def _resolve_context(
    session: Session,
    profile_id: str | None,
    locations: list[str] | None,
) -> tuple[list[LocationCfg], RuleSet, ProfileConfig | None]:
    """Figure out which locations + ruleset to use.

    Precedence: explicit `location` query params always win. If a profile is
    named, its locations are the default and its rule overrides are merged
    on top of the base `rules.yml`.
    """
    profile_cfg: ProfileConfig | None = None
    if profile_id:
        row = get_profile(session, profile_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"profile '{profile_id}' not found")
        profile_cfg = ProfileConfig(**(row.config or {}))

    loc_ids = locations or (profile_cfg.locations if profile_cfg else [])
    if not loc_ids:
        raise HTTPException(
            status_code=400,
            detail="No location specified. Pass `location=...` or a profile with locations.",
        )
    resolved = [get_location(l) for l in loc_ids]
    rules = effective_ruleset(profile_cfg)
    return resolved, rules, profile_cfg


# ---------- endpoints ----------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/locations", response_model=list[LocationOut])
def locations() -> list[LocationOut]:
    return [LocationOut.from_cfg(l) for l in load_locations()]


@app.get("/ingest-status", response_model=IngestStatus)
def ingest_status(db: Session = Depends(get_db)) -> IngestStatus:
    """Current database freshness by location."""
    statuses: list[LocationIngestStatus] = []
    for loc in load_locations():
        marine_count, latest_marine_valid_to, latest_marine_fetched_at = db.execute(
            select(
                func.count(MarineForecast.id),
                func.max(MarineForecast.valid_to),
                func.max(MarineForecast.fetched_at),
            ).where(MarineForecast.location_id == loc.id)
        ).one()
        tide_count, latest_tide_time = db.execute(
            select(
                func.count(TidePrediction.id),
                func.max(TidePrediction.t),
            ).where(TidePrediction.location_id == loc.id)
        ).one()
        forecast_count, latest_forecast_time, latest_forecast_fetched_at = db.execute(
            select(
                func.count(HourlyForecast.id),
                func.max(HourlyForecast.t),
                func.max(HourlyForecast.fetched_at),
            ).where(HourlyForecast.location_id == loc.id)
        ).one()
        statuses.append(
            LocationIngestStatus(
                location_id=loc.id,
                latest_hourly_time=latest_forecast_time,
                latest_hourly_fetched_at=latest_forecast_fetched_at,
                hourly_rows=forecast_count,
                latest_tide_time=latest_tide_time,
                tide_rows=tide_count,
                latest_marine_valid_to=latest_marine_valid_to,
                latest_marine_fetched_at=latest_marine_fetched_at,
                marine_rows=marine_count,
            )
        )
    return IngestStatus(generated_at=datetime.now(timezone.utc), locations=statuses)


@app.get("/conditions", response_model=list[HourOut])
def conditions(
    location: str | None = Query(None, description="Location id, e.g. 'kings_point'."),
    profile: str | None = Query(None, description="Profile id (supplies location + rule overrides)."),
    when: str = Query("today", description="today | tomorrow | this-week | YYYY-MM-DD"),
    db: Session = Depends(get_db),
) -> list[HourOut]:
    locs, rules, _ = _resolve_context(db, profile, [location] if location else None)
    loc = locs[0]  # single-location endpoint
    start, end, _ = _resolve_when(when, loc)
    reports = score_range(db, loc, start, end, rules)
    return [HourOut.from_report(r) for r in reports]


@app.get("/sail-windows", response_model=list[WindowOut])
def sail_windows(
    location: str | None = Query(None, description="Location id."),
    profile: str | None = Query(None, description="Profile id."),
    when: str = Query("this-week"),
    db: Session = Depends(get_db),
) -> list[WindowOut]:
    """Every contiguous span of daylight hours meeting the (per-profile) score threshold."""
    locs, rules, _ = _resolve_context(db, profile, [location] if location else None)
    loc = locs[0]
    start, end, _ = _resolve_when(when, loc)
    reports = score_range(db, loc, start, end, rules)
    windows = find_windows(reports, rules)
    windows.sort(key=lambda w: w.avg_score, reverse=True)
    return [WindowOut.from_window(w) for w in windows]


@app.get("/summary/{when}", response_model=DaySummary)
def summary(
    when: str,
    location: str | None = Query(None, description="Location id."),
    profile: str | None = Query(None, description="Profile id."),
    db: Session = Depends(get_db),
) -> DaySummary:
    """High-level verdict for a day. Ideal for an agent answering 'is X good?'."""
    locs, rules, _ = _resolve_context(db, profile, [location] if location else None)
    loc = locs[0]
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


@app.get(
    "/calendar.ics",
    responses={200: {"content": {"text/calendar": {}}}},
)
@app.get("/calendar", include_in_schema=False)
@app.get("/ics", include_in_schema=False)
def calendar_ics(
    location: list[str] | None = Query(
        None, description="Location id(s). Repeat for multi-location calendars."
    ),
    profile: str | None = Query(None, description="Profile id (supplies locations + rule overrides)."),
    days: int | None = Query(None, ge=1, le=14, description="Lookahead in days."),
    min_verdict: Literal["maybe", "go"] | None = Query(
        None, description="Drop windows weaker than this verdict."
    ),
    include_tides: bool | None = Query(None, description="Add high/low tide events."),
    db: Session = Depends(get_db),
) -> Response:
    """Subscribable ICS feed. Add the URL to Google Calendar → 'From URL'."""
    locs, rules, profile_cfg = _resolve_context(db, profile, location)

    # Profile supplies defaults; explicit query params override.
    eff_days = days if days is not None else 7
    eff_min_verdict = min_verdict or (profile_cfg.min_verdict if profile_cfg else None) or "maybe"
    eff_include_tides = include_tides if include_tides is not None else (
        profile_cfg.include_tides if profile_cfg else False
    )

    now = datetime.now(timezone.utc)
    start = now.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(days=eff_days)

    windows_out: list[tuple[LocationCfg, SailWindow, str]] = []
    tides_out: list[tuple[LocationCfg, datetime, float, str]] = []

    for loc in locs:
        reports = score_range(db, loc, start, end, rules)
        for w in find_windows(reports, rules):
            v = verdict_for_window(w)
            if eff_min_verdict == "go" and v != "go":
                continue
            windows_out.append((loc, w, v))

        if eff_include_tides:
            rows = db.execute(
                select(TidePrediction)
                .where(TidePrediction.location_id == loc.id)
                .where(TidePrediction.type.isnot(None))
                .where(TidePrediction.t >= start)
                .where(TidePrediction.t < end)
                .order_by(TidePrediction.t)
            ).scalars().all()
            for r in rows:
                t = r.t if r.t.tzinfo else r.t.replace(tzinfo=timezone.utc)
                tides_out.append((loc, t, r.height_m, r.type or ""))

    body = build_calendar(
        locations_and_windows=windows_out,
        tide_events=tides_out,
    )
    return Response(
        content=body,
        media_type="text/calendar",
        headers={"Content-Disposition": 'inline; filename="sail-soon.ics"'},
    )


# ---------- profiles ----------


@app.post("/profiles", response_model=ProfileCreated, status_code=201)
def create_profile_endpoint(payload: ProfileIn, db: Session = Depends(get_db)) -> ProfileCreated:
    """Create a new filter profile. Response includes a one-time `edit_token` —
    save it somewhere safe; you'll need it to edit or delete."""
    # Validate that referenced locations exist.
    for l in payload.config.locations:
        try:
            get_location(l)
        except KeyError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
    try:
        created = create_profile(db, payload)
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    db.commit()
    return created


@app.get("/profiles", response_model=list[ProfileOut])
def list_profiles_endpoint(db: Session = Depends(get_db)) -> list[ProfileOut]:
    return list_profiles(db)


@app.get("/profiles/{profile_id}", response_model=ProfileOut)
def get_profile_endpoint(profile_id: str, db: Session = Depends(get_db)) -> ProfileOut:
    row = get_profile(db, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return ProfileOut(
        id=row.id,
        name=row.name,
        config=ProfileConfig(**(row.config or {})),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@app.put("/profiles/{profile_id}", response_model=ProfileOut)
def update_profile_endpoint(
    profile_id: str,
    patch: ProfileUpdate,
    db: Session = Depends(get_db),
    x_edit_token: str = Header(..., alias="X-Edit-Token"),
) -> ProfileOut:
    if patch.config is not None:
        for l in patch.config.locations:
            try:
                get_location(l)
            except KeyError as err:
                raise HTTPException(status_code=400, detail=str(err)) from err
    try:
        out = update_profile(db, profile_id, x_edit_token, patch)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except PermissionError as err:
        raise HTTPException(status_code=403, detail=str(err)) from err
    db.commit()
    return out


@app.delete("/profiles/{profile_id}", status_code=204)
def delete_profile_endpoint(
    profile_id: str,
    db: Session = Depends(get_db),
    x_edit_token: str = Header(..., alias="X-Edit-Token"),
) -> Response:
    try:
        delete_profile(db, profile_id, x_edit_token)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except PermissionError as err:
        raise HTTPException(status_code=403, detail=str(err)) from err
    db.commit()
    return Response(status_code=204)


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
