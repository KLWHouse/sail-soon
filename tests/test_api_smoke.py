"""Minimal smoke test: migrate in-memory SQLite, spin up FastAPI, hit endpoints."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ["SAILSOON_DATABASE_URL"] = "sqlite:///./_test_sailsoon.db"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # Clean DB per run.
    db_path = "./_test_sailsoon.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    from sailsoon.db import get_engine
    from sailsoon.models import Base

    Base.metadata.create_all(get_engine())

    from sailsoon.api import app
    from sailsoon.sync import sync_locations

    sync_locations()

    # Seed a handful of hourly rows so /summary has something to chew on.
    from sailsoon.db import session_scope
    from sailsoon.models import HourlyForecast

    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    with session_scope() as s:
        for i in range(24):
            s.add(
                HourlyForecast(
                    location_id="kings_point",
                    t=start + timedelta(hours=i),
                    wind_speed_kt=12.0,
                    wind_gust_kt=16.0,
                    wind_dir_deg=220.0,
                    wave_height_m=0.4,
                    precip_prob_pct=10.0,
                    air_temp_c=18.0,
                    cloud_cover_pct=40.0,
                    fetched_at=start,
                )
            )
    with TestClient(app) as c:
        yield c

    if os.path.exists(db_path):
        os.remove(db_path)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_locations_has_kings_point(client):
    ids = [l["id"] for l in client.get("/locations").json()]
    assert "kings_point" in ids


def test_conditions_returns_hours(client):
    r = client.get("/conditions", params={"location": "kings_point", "when": "today"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_summary_shape(client):
    r = client.get("/summary/today", params={"location": "kings_point"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] in {"go", "maybe", "no-go", "no-data"}
    assert body["location_id"] == "kings_point"
