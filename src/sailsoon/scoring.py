from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun
from sqlalchemy import select

from .config import Location, Rule, RuleSet, load_rules
from .models import HourlyForecast, MarineForecast


@dataclass
class HourReport:
    t: datetime
    score: float
    label: str  # good | marginal | bad
    daylight: bool
    reasons: dict[str, float]
    hazards: list[str]
    raw: dict[str, float | None]


@dataclass
class SailWindow:
    start: datetime
    end: datetime
    avg_score: float
    min_score: float
    hours: list[HourReport] = field(default_factory=list)

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


def _trapezoid(v: float | None, r: Rule) -> float:
    if v is None:
        return 0.5  # no data = neutral; don't reward or punish
    lz, lf, hf, hz = r.low_zero, r.low_full, r.high_full, r.high_zero
    assert lz is not None and lf is not None and hf is not None and hz is not None
    if v <= lz or v >= hz:
        return 0.0
    if lf <= v <= hf:
        return 1.0
    if lz < v < lf:
        return (v - lz) / (lf - lz)
    return (hz - v) / (hz - hf)  # hf < v < hz


def _ceiling(v: float | None, r: Rule) -> float:
    if v is None:
        return 0.75
    warn, fail = r.warn_at, r.fail_at
    assert warn is not None and fail is not None
    if v <= warn:
        return 1.0
    if v >= fail:
        return 0.0
    return 1.0 - (v - warn) / (fail - warn)


def _hazard(hazards: list[str], r: Rule) -> float:
    if any(h in hazards for h in r.fail_on):
        return 0.0
    return 1.0


def _score_hour(
    h: HourlyForecast,
    hazards_for_hour: list[str],
    rules: RuleSet,
) -> tuple[float, dict[str, float]]:
    reasons: dict[str, float] = {}
    total_w = 0.0
    total = 0.0
    raw_sources = {
        "wind_speed_kt": h.wind_speed_kt,
        "wind_gust_kt": h.wind_gust_kt,
        "wave_height_m": h.wave_height_m,
        "precip_prob_pct": h.precip_prob_pct,
        "hazards": hazards_for_hour,
    }
    for name, rule in rules.rules.items():
        val = raw_sources.get(rule.source)
        if rule.type == "trapezoid":
            s = _trapezoid(val, rule)
        elif rule.type == "ceiling":
            s = _ceiling(val, rule)
        elif rule.type == "nws_hazard":
            s = _hazard(hazards_for_hour, rule)
        else:
            continue
        reasons[name] = round(s, 3)
        total += s * rule.weight
        total_w += rule.weight
    score = (total / total_w) if total_w else 0.0
    return score, reasons


def _daylight(loc: Location, d: date) -> tuple[datetime, datetime]:
    info = LocationInfo(loc.name, "US", loc.timezone, loc.latitude, loc.longitude)
    s = sun(info.observer, date=d, tzinfo=ZoneInfo(loc.timezone))
    return s["sunrise"].astimezone(timezone.utc), s["sunset"].astimezone(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on read; assume stored values are UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hazards_at(t: datetime, marine: list[MarineForecast]) -> list[str]:
    for mf in marine:
        if _as_utc(mf.valid_from) <= t < _as_utc(mf.valid_to):
            return list(mf.hazards or [])
    return []


def score_range(
    session,
    loc: Location,
    start: datetime,
    end: datetime,
    rules: RuleSet | None = None,
) -> list[HourReport]:
    rules = rules or load_rules()
    hourly = (
        session.execute(
            select(HourlyForecast)
            .where(HourlyForecast.location_id == loc.id)
            .where(HourlyForecast.t >= start)
            .where(HourlyForecast.t < end)
            .order_by(HourlyForecast.t)
        )
        .scalars()
        .all()
    )
    marine = (
        session.execute(
            select(MarineForecast)
            .where(MarineForecast.location_id == loc.id)
            .where(MarineForecast.valid_to > start)
            .where(MarineForecast.valid_from < end)
        )
        .scalars()
        .all()
    )

    reports: list[HourReport] = []
    dw = rules.daylight_window
    # Pre-compute day bounds per local date to avoid repeated astral calls.
    local_tz = ZoneInfo(loc.timezone)
    day_bounds: dict[date, tuple[datetime, datetime]] = {}

    for h in hourly:
        ht = _as_utc(h.t)
        hazards = _hazards_at(ht, marine)
        score, reasons = _score_hour(h, hazards, rules)

        local_date = ht.astimezone(local_tz).date()
        if local_date not in day_bounds:
            sunrise, sunset = _daylight(loc, local_date)
            day_bounds[local_date] = (
                sunrise + timedelta(minutes=dw.sunrise_buffer_minutes),
                sunset + timedelta(minutes=dw.sunset_buffer_minutes),
            )
        day_start, day_end = day_bounds[local_date]
        is_day = day_start <= ht < day_end

        if score >= 0.75:
            label = "good"
        elif score >= rules.min_score:
            label = "marginal"
        else:
            label = "bad"

        reports.append(
            HourReport(
                t=ht,
                score=round(score, 3),
                label=label,
                daylight=is_day,
                reasons=reasons,
                hazards=hazards,
                raw={
                    "wind_speed_kt": h.wind_speed_kt,
                    "wind_gust_kt": h.wind_gust_kt,
                    "wind_dir_deg": h.wind_dir_deg,
                    "wave_height_m": h.wave_height_m,
                    "precip_prob_pct": h.precip_prob_pct,
                    "air_temp_c": h.air_temp_c,
                    "cloud_cover_pct": h.cloud_cover_pct,
                },
            )
        )
    return reports


def find_windows(reports: list[HourReport], rules: RuleSet) -> list[SailWindow]:
    windows: list[SailWindow] = []
    current: list[HourReport] = []

    def flush():
        if len(current) >= rules.min_duration_hours:
            scores = [r.score for r in current]
            windows.append(
                SailWindow(
                    start=current[0].t,
                    end=current[-1].t + timedelta(hours=1),
                    avg_score=round(sum(scores) / len(scores), 3),
                    min_score=round(min(scores), 3),
                    hours=list(current),
                )
            )
        current.clear()

    for r in reports:
        ok = r.daylight and r.score >= rules.min_score
        if ok:
            current.append(r)
        else:
            flush()
    flush()
    return windows


def day_range(loc: Location, target: date) -> tuple[datetime, datetime]:
    """Local-day bounds in UTC for the given location's timezone."""
    tz = ZoneInfo(loc.timezone)
    start_local = datetime.combine(target, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
