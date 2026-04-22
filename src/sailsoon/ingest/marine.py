from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from dateutil import parser as dtparse

from ..config import Location, load_locations
from ..db import session_scope
from ..models import MarineForecast
from ._http import client

# NWS doesn't serve a zone-forecast JSON for marine zones — that endpoint is
# land-only. The authoritative source for marine hazards (Small Craft Advisory,
# Gale Warning, etc.) is the active-alerts endpoint filtered by zone.
# https://www.weather.gov/documentation/services-web-api
ALERTS_URL = "https://api.weather.gov/alerts/active"

# Mirrors the `event` strings NWS uses so rules.yml can match them directly.
KNOWN_MARINE_HAZARDS = {
    "Small Craft Advisory",
    "Gale Warning",
    "Storm Warning",
    "Hurricane Warning",
    "Tropical Storm Warning",
    "Special Marine Warning",
    "Hazardous Seas Warning",
    "Dense Fog Advisory",
    "Freezing Spray Advisory",
    "Marine Weather Statement",
}


def _fetch_alerts(zone: str) -> dict:
    with client() as c:
        r = c.get(ALERTS_URL, params={"zone": zone})
        r.raise_for_status()
        return r.json()


def _rows_for_location(loc: Location) -> Iterable[dict]:
    data = _fetch_alerts(loc.marine_zone)
    fetched_at = datetime.now(timezone.utc)
    features = data.get("features") or []
    for feat in features:
        props = feat.get("properties") or {}
        event = props.get("event") or "Marine Alert"

        # onset/ends can be null; fall back sensibly so the row still covers a
        # real time range.
        raw_start = props.get("onset") or props.get("effective") or props.get("sent")
        raw_end = props.get("ends") or props.get("expires")
        if not raw_start or not raw_end:
            continue
        start = dtparse.isoparse(raw_start).astimezone(timezone.utc)
        end = dtparse.isoparse(raw_end).astimezone(timezone.utc)

        yield {
            "location_id": loc.id,
            "zone": loc.marine_zone,
            "valid_from": start,
            "valid_to": end,
            "headline": props.get("headline") or event,
            "hazards": [event],
            "raw_text": props.get("description"),
            "fetched_at": fetched_at,
        }


def _replace_for_zone(session, zone: str, rows: list[dict]) -> int:
    # Alerts can be withdrawn at any time, so replace wholesale rather than
    # upserting.
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
