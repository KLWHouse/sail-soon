"""Profile CRUD + filter-override behavior end-to-end."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from sailsoon import config as config_module
    from sailsoon import db as db_module

    db_path = "./_test_profiles.db"
    os.environ["SAILSOON_DATABASE_URL"] = f"sqlite:///{db_path}"
    config_module.get_settings.cache_clear()
    if db_module._engine is not None:
        db_module._engine.dispose()
    db_module._engine = None
    db_module._SessionLocal = None

    if os.path.exists(db_path):
        os.remove(db_path)

    from sailsoon.db import get_engine, session_scope
    from sailsoon.models import Base, HourlyForecast

    Base.metadata.create_all(get_engine())

    from sailsoon.sync import sync_locations

    sync_locations()

    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    with session_scope() as s:
        for i in range(48):
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
            s.add(
                HourlyForecast(
                    location_id="new_haven",
                    t=start + timedelta(hours=i),
                    wind_speed_kt=6.0,  # too light for default profile
                    wind_gust_kt=10.0,
                    wind_dir_deg=180.0,
                    wave_height_m=0.2,
                    precip_prob_pct=5.0,
                    air_temp_c=18.0,
                    cloud_cover_pct=20.0,
                    fetched_at=start,
                )
            )

    from sailsoon.api import app
    with TestClient(app) as c:
        yield c

    if db_module._engine is not None:
        db_module._engine.dispose()
    db_module._engine = None
    db_module._SessionLocal = None
    if os.path.exists(db_path):
        os.remove(db_path)


def _create(client, **extra):
    payload = {
        "id": "brian-dinghy",
        "name": "Brian's dinghy",
        "config": {
            "locations": ["kings_point"],
            "min_score": 0.5,
        },
        **extra,
    }
    return client.post("/profiles", json=payload)


def test_create_and_get_profile(client):
    r = _create(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "brian-dinghy"
    assert "edit_token" in body and len(body["edit_token"]) > 20

    r2 = client.get("/profiles/brian-dinghy")
    assert r2.status_code == 200
    assert r2.json()["name"] == "Brian's dinghy"
    assert "edit_token" not in r2.json()

    # Duplicate slug -> 409
    r3 = _create(client)
    assert r3.status_code == 409


def test_slug_validation(client):
    r = client.post(
        "/profiles",
        json={"id": "BadSlug!", "name": "x", "config": {"locations": ["kings_point"]}},
    )
    assert r.status_code == 422


def test_unknown_location_rejected(client):
    r = client.post(
        "/profiles",
        json={"id": "nope-test", "name": "x", "config": {"locations": ["atlantis"]}},
    )
    assert r.status_code == 400


def test_update_requires_token(client):
    token = _profile_token(client, "dinghy-2", locations=["kings_point"])

    # Without token -> 422 (missing required header)
    r = client.put("/profiles/dinghy-2", json={"name": "renamed"})
    assert r.status_code == 422

    # Wrong token -> 403
    r = client.put(
        "/profiles/dinghy-2",
        json={"name": "renamed"},
        headers={"X-Edit-Token": "wrong"},
    )
    assert r.status_code == 403

    # Right token -> 200
    r = client.put(
        "/profiles/dinghy-2",
        json={"name": "renamed"},
        headers={"X-Edit-Token": token},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "renamed"


def _profile_token(client, slug: str, **config_kwargs) -> str:
    r = client.post(
        "/profiles",
        json={
            "id": slug,
            "name": slug,
            "config": {"locations": config_kwargs.get("locations", ["kings_point"]), **{
                k: v for k, v in config_kwargs.items() if k != "locations"
            }},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["edit_token"]


def test_profile_drives_sail_windows_location(client):
    _profile_token(client, "kings-only", locations=["kings_point"])
    # No ?location=, profile supplies it.
    r = client.get("/sail-windows", params={"profile": "kings-only", "when": "today"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_profile_rule_override_changes_verdict(client):
    # Baseline: default rules at kings_point (steady 12 kt).
    baseline = client.get("/summary/today", params={"location": "kings_point"}).json()

    # Strict profile: require 20-25 kt wind (unusual). 12 kt falls well below
    # the ideal band, so the score should tank.
    _profile_token(
        client,
        "heavy-air",
        locations=["kings_point"],
        rule_overrides={
            "wind_speed": {"low_zero": 18, "low_full": 20, "high_full": 25, "high_zero": 30}
        },
    )
    strict = client.get("/summary/today", params={"profile": "heavy-air"}).json()

    # Whatever the baseline verdict, the strict override should score lower.
    def _best_score(body):
        bw = body.get("best_window")
        return bw["avg_score"] if bw else 0.0

    assert _best_score(strict) < _best_score(baseline)


def test_calendar_uses_profile_locations(client):
    _profile_token(
        client,
        "multi-loc",
        locations=["kings_point", "new_haven"],
        min_verdict="maybe",
    )
    r = client.get("/calendar.ics", params={"profile": "multi-loc"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert r.text.startswith("BEGIN:VCALENDAR")


def test_delete_profile(client):
    token = _profile_token(client, "throwaway", locations=["kings_point"])
    r = client.delete("/profiles/throwaway", headers={"X-Edit-Token": token})
    assert r.status_code == 204
    assert client.get("/profiles/throwaway").status_code == 404


def test_missing_location_returns_400(client):
    r = client.get("/sail-windows", params={"when": "today"})
    assert r.status_code == 400
