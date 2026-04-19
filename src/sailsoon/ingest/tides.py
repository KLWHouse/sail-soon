from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..config import Location, load_locations
from ..db import get_engine, session_scope
from ..models import TidePrediction
from ._http import client

# CO-OPS data getter. Predictions use datum MLLW, metric units, GMT.
# https://api.tidesandcurrents.noaa.gov/api/prod/
BASE_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


def _fetch_predictions(station: str, begin: datetime, end: datetime, interval: str) -> list[dict]:
    """interval = "hilo" for extrema, "h" for hourly."""
    params = {
        "product": "predictions",
        "application": "sail-soon",
        "datum": "MLLW",
        "units": "metric",
        "time_zone": "gmt",
        "format": "json",
        "station": station,
        "begin_date": begin.strftime("%Y%m%d %H:%M"),
        "end_date": end.strftime("%Y%m%d %H:%M"),
        "interval": interval,
    }
    with client() as c:
        r = c.get(BASE_URL, params=params)
        r.raise_for_status()
        data = r.json()
    if "error" in data:
        raise RuntimeError(f"CO-OPS error for {station}: {data['error']}")
    return data.get("predictions", [])


def _rows_for_location(loc: Location, days: int) -> Iterable[dict]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(days=days)

    # High/low extrema — useful for calendar-style output.
    for p in _fetch_predictions(loc.tide_station, now, end, "hilo"):
        yield {
            "location_id": loc.id,
            "t": datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc),
            "height_m": float(p["v"]),
            "type": p.get("type"),
        }

    # Hourly predictions — useful for scoring.
    for p in _fetch_predictions(loc.tide_station, now, end, "h"):
        yield {
            "location_id": loc.id,
            "t": datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc),
            "height_m": float(p["v"]),
            "type": None,
        }


def _upsert(session, rows: list[dict]) -> int:
    if not rows:
        return 0
    dialect = get_engine().dialect.name
    table = TidePrediction.__table__
    if dialect == "postgresql":
        stmt = pg_insert(table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["location_id", "t", "type"],
            set_={"height_m": stmt.excluded.height_m},
        )
    elif dialect == "sqlite":
        stmt = sqlite_insert(table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["location_id", "t", "type"],
            set_={"height_m": stmt.excluded.height_m},
        )
    else:
        raise RuntimeError(f"Unsupported DB dialect: {dialect}")
    session.execute(stmt)
    return len(rows)


def ingest_tides(location_ids: list[str] | None = None, days: int = 7) -> dict[str, int]:
    """Fetch tide predictions for every (or selected) location; return counts per location."""
    locations = load_locations()
    if location_ids:
        locations = [l for l in locations if l.id in location_ids]
    counts: dict[str, int] = {}
    with session_scope() as session:
        for loc in locations:
            rows = list(_rows_for_location(loc, days))
            counts[loc.id] = _upsert(session, rows)
    return counts
