"""Minimal smoke test: migrate in-memory SQLite, spin up FastAPI, hit endpoints."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # Clean DB per run.
    db_path = "./_test_sailsoon.db"
    os.environ["SAILSOON_DATABASE_URL"] = f"sqlite:///{db_path}"
    if os.path.exists(db_path):
        os.remove(db_path)

    from sailsoon import config as config_module
    config_module.get_settings.cache_clear()

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


def test_ingest_status_reports_forecast_freshness(client):
    r = client.get("/ingest-status")
    assert r.status_code == 200
    body = r.json()
    kings_point = next(l for l in body["locations"] if l["location_id"] == "kings_point")
    assert kings_point["hourly_rows"] == 24
    assert kings_point["latest_hourly_time"] is not None
    assert kings_point["latest_hourly_fetched_at"] is not None


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


def test_calendar_ics_serves_text_calendar(client):
    r = client.get("/calendar.ics", params={"location": "kings_point", "days": 1})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR")
    assert "END:VCALENDAR" in body
    # Seeded data is steady 12kt wind, so at least one window should be emitted.
    assert "BEGIN:VEVENT" in body
    assert "Kings Point" in body


@pytest.mark.parametrize("path", ["/calendar", "/ics"])
def test_calendar_aliases_serve_text_calendar(client, path):
    r = client.get(path, params={"location": "kings_point", "days": 1})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert r.text.startswith("BEGIN:VCALENDAR")


def test_calendar_ics_multi_location(client):
    r = client.get(
        "/calendar.ics",
        params=[("location", "kings_point"), ("location", "bridgeport"), ("days", 1)],
    )
    assert r.status_code == 200
    assert r.text.startswith("BEGIN:VCALENDAR")
