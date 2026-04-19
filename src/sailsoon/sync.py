"""Sync the `locations` table to match config/locations.yml."""
from __future__ import annotations

from sqlalchemy import select

from .config import load_locations
from .db import session_scope
from .models import Location as LocationModel


def sync_locations() -> int:
    cfg = load_locations()
    with session_scope() as session:
        existing = {l.id: l for l in session.execute(select(LocationModel)).scalars()}
        count = 0
        for loc in cfg:
            row = existing.get(loc.id)
            if row is None:
                session.add(
                    LocationModel(
                        id=loc.id,
                        name=loc.name,
                        tide_station=loc.tide_station,
                        marine_zone=loc.marine_zone,
                        latitude=loc.latitude,
                        longitude=loc.longitude,
                        timezone=loc.timezone,
                    )
                )
            else:
                row.name = loc.name
                row.tide_station = loc.tide_station
                row.marine_zone = loc.marine_zone
                row.latitude = loc.latitude
                row.longitude = loc.longitude
                row.timezone = loc.timezone
            count += 1
    return count
