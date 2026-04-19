from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from dateutil import parser as dtparse

from ..config import Location, load_locations
from ..db import session_scope
from ..models import MarineForecast
from ._http import client

# NWS zone forecast endpoint. Returns JSON with a "periods" array; each period
# has a name, detailed forecast text, and start/end times.
# https://www.weather.gov/documentation/services-web-api
BASE_URL = "https://api.weather.gov/zones/forecast/{zone}/forecast"

HAZARD_PATTERNS = [
    "Small Craft Advisory",
    "Gale Warning",
    "Storm Warning",
    "Hurricane Warning",
    "Special Marine Warning",
    "Dense Fog Advisory",
]


def _extract_hazards(text: str) -> list[str]:
    hits = []
    for h in HAZARD_PATTERNS:
        if re.search(h, text, re.IGNORECASE):
            hits.append(h)
    return hits


def _fetch_zone(zone: str) -> dict:
    with client() as c:
        r = c.get(BASE_URL.format(zone=zone))
        r.raise_for_status()
        return r.json()


def _rows_for_location(loc: Location) -> Iterable[dict]:
    data = _fetch_zone(loc.marine_zone)
    fetched_at = datetime.now(timezone.utc)
    props = data.get("properties") or {}
    periods = props.get("periods") or []
    for p in periods:
        start = dtparse.isoparse(p["startTime"]).astimezone(timezone.utc)
        end = dtparse.isoparse(p["endTime"]).astimezone(timezone.utc)
        text = p.get("detailedForecast") or ""
        yield {
            "location_id": loc.id,
            "zone": loc.marine_zone,
            "valid_from": start,
            "valid_to": end,
            "headline": p.get("name"),
            "hazards": _extract_hazards(text),
            "raw_text": text,
            "fetched_at": fetched_at,
        }


def _replace_for_zone(session, zone: str, rows: list[dict]) -> int:
    # Forecasts change on every fetch; simplest correct thing is to delete and
    # reinsert for the zone.
    session.query(MarineForecast).filter(MarineForecast.zone == zone).delete()
    if rows:
        session.bulk_insert_mappings(MarineForecast, rows)
    return len(rows)


def ingest_marine(location_ids: list[str] | None = None) -> dict[str, int]:
    locations = load_locations()
    if location_ids:
        locations = [l for l in locations if l.id in location_ids]

    # Multiple locations can share a zone; fetch each zone once.
    seen_zones: set[str] = set()
    counts: dict[str, int] = {}
    with session_scope() as session:
        for loc in locations:
            if loc.marine_zone in seen_zones:
                counts[loc.id] = 0
                continue
            rows = list(_rows_for_location(loc))
            counts[loc.id] = _replace_for_zone(session, loc.marine_zone, rows)
            seen_zones.add(loc.marine_zone)
    return counts
