from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from dateutil import parser as dtparse
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..config import Location, get_settings, load_locations
from ..db import get_engine, session_scope
from ..models import HourlyForecast
from ._http import client

# Open-Meteo: free, no key. Two endpoints — atmospheric + marine. We merge them
# on matching UTC hour.
# https://open-meteo.com/en/docs
# https://open-meteo.com/en/docs/marine-weather-api
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"

MS_TO_KT = 1.9438445  # m/s -> knots (Open-Meteo default wind unit)


def _fetch_weather(lat: float, lon: float, days: int) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(
            [
                "wind_speed_10m",
                "wind_gusts_10m",
                "wind_direction_10m",
                "precipitation_probability",
                "temperature_2m",
                "cloud_cover",
            ]
        ),
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "forecast_days": days,
    }
    with client() as c:
        r = c.get(WEATHER_URL, params=params)
        r.raise_for_status()
        return r.json()


def _fetch_marine(lat: float, lon: float, days: int) -> dict | None:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(["wave_height", "swell_wave_period"]),
        "timezone": "UTC",
        "forecast_days": days,
    }
    with client() as c:
        r = c.get(MARINE_URL, params=params)
        # Marine model doesn't cover every inland station; treat 400 as "no data".
        if r.status_code == 400:
            return None
        r.raise_for_status()
        return r.json()


def _rows_for_location(loc: Location, days: int) -> Iterable[dict]:
    weather = _fetch_weather(loc.latitude, loc.longitude, days)
    marine = _fetch_marine(loc.latitude, loc.longitude, days)
    fetched_at = datetime.now(timezone.utc)

    times = weather["hourly"]["time"]
    ws = weather["hourly"]["wind_speed_10m"]
    wg = weather["hourly"]["wind_gusts_10m"]
    wd = weather["hourly"]["wind_direction_10m"]
    pp = weather["hourly"]["precipitation_probability"]
    tc = weather["hourly"]["temperature_2m"]
    cc = weather["hourly"]["cloud_cover"]

    marine_by_time: dict[str, tuple[float | None, float | None]] = {}
    if marine:
        m_times = marine["hourly"]["time"]
        wh = marine["hourly"]["wave_height"]
        sp = marine["hourly"]["swell_wave_period"]
        for t, w, s in zip(m_times, wh, sp):
            marine_by_time[t] = (w, s)

    for i, t in enumerate(times):
        dt = dtparse.isoparse(t).replace(tzinfo=timezone.utc)
        wave_h, swell_p = marine_by_time.get(t, (None, None))
        yield {
            "location_id": loc.id,
            "t": dt,
            "wind_speed_kt": ws[i],
            "wind_gust_kt": wg[i],
            "wind_dir_deg": wd[i],
            "wave_height_m": wave_h,
            "swell_period_s": swell_p,
            "precip_prob_pct": pp[i],
            "air_temp_c": tc[i],
            "cloud_cover_pct": cc[i],
            "fetched_at": fetched_at,
        }


def _upsert(session, rows: list[dict]) -> int:
    if not rows:
        return 0
    dialect = get_engine().dialect.name
    table = HourlyForecast.__table__
    update_cols = [
        "wind_speed_kt",
        "wind_gust_kt",
        "wind_dir_deg",
        "wave_height_m",
        "swell_period_s",
        "precip_prob_pct",
        "air_temp_c",
        "cloud_cover_pct",
        "fetched_at",
    ]
    if dialect == "postgresql":
        stmt = pg_insert(table).values(rows)
    elif dialect == "sqlite":
        stmt = sqlite_insert(table).values(rows)
    else:
        raise RuntimeError(f"Unsupported DB dialect: {dialect}")
    stmt = stmt.on_conflict_do_update(
        index_elements=["location_id", "t"],
        set_={c: getattr(stmt.excluded, c) for c in update_cols},
    )
    session.execute(stmt)
    return len(rows)


def ingest_openmeteo(location_ids: list[str] | None = None, days: int | None = None) -> dict[str, int]:
    locations = load_locations()
    if location_ids:
        locations = [l for l in locations if l.id in location_ids]
    days = days or get_settings().forecast_days
    counts: dict[str, int] = {}
    with session_scope() as session:
        for loc in locations:
            rows = list(_rows_for_location(loc, days))
            counts[loc.id] = _upsert(session, rows)
    return counts
