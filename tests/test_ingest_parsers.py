"""Parser tests that don't hit the network — use respx to mock the upstream HTTP."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
import respx
from httpx import Response

@pytest.fixture(autouse=True)
def _setup_db():
    from sailsoon import config as config_module
    from sailsoon import db as db_module

    db_path = "./_test_parsers.db"
    os.environ["SAILSOON_DATABASE_URL"] = f"sqlite:///{db_path}"
    config_module.get_settings.cache_clear()
    if db_module._engine is not None:
        db_module._engine.dispose()
    db_module._engine = None
    db_module._SessionLocal = None

    if os.path.exists(db_path):
        os.remove(db_path)

    from sailsoon.db import get_engine
    from sailsoon.models import Base
    Base.metadata.create_all(get_engine())
    from sailsoon.sync import sync_locations
    sync_locations()
    yield
    if db_module._engine is not None:
        db_module._engine.dispose()
    db_module._engine = None
    db_module._SessionLocal = None
    if os.path.exists(db_path):
        os.remove(db_path)


@respx.mock
def test_tide_ingester_parses_noaa_response():
    from sailsoon.ingest.tides import BASE_URL, ingest_tides

    payload_hilo = {
        "predictions": [
            {"t": "2026-04-20 06:12", "v": "0.123", "type": "L"},
            {"t": "2026-04-20 12:34", "v": "1.987", "type": "H"},
        ]
    }
    payload_h = {
        "predictions": [
            {"t": "2026-04-20 00:00", "v": "1.2"},
            {"t": "2026-04-20 01:00", "v": "1.0"},
        ]
    }
    # Route by `interval` query param — first call hilo, then h.
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        interval = dict(request.url.params).get("interval")
        return Response(200, json=payload_hilo if interval == "hilo" else payload_h)

    respx.get(BASE_URL).mock(side_effect=handler)
    counts = ingest_tides(location_ids=["kings_point"], days=1)
    # 2 hilo + 2 hourly per location.
    assert counts["kings_point"] == 4


@respx.mock
def test_openmeteo_ingester_parses_response():
    from sailsoon.ingest.openmeteo import MARINE_URL, WEATHER_URL, ingest_openmeteo

    weather_payload = {
        "hourly": {
            "time": ["2026-04-20T00:00", "2026-04-20T01:00"],
            "wind_speed_10m": [10.0, 12.0],
            "wind_gusts_10m": [14.0, 16.0],
            "wind_direction_10m": [200.0, 210.0],
            "precipitation_probability": [5, 10],
            "temperature_2m": [12.0, 12.5],
            "cloud_cover": [20, 30],
        }
    }
    marine_payload = {
        "hourly": {
            "time": ["2026-04-20T00:00", "2026-04-20T01:00"],
            "wave_height": [0.3, 0.4],
            "swell_wave_period": [5.0, 5.1],
        }
    }
    respx.get(WEATHER_URL).mock(return_value=Response(200, json=weather_payload))
    respx.get(MARINE_URL).mock(return_value=Response(200, json=marine_payload))

    counts = ingest_openmeteo(location_ids=["kings_point"], days=1)
    assert counts["kings_point"] == 2

    # Verify data landed correctly.
    from sqlalchemy import select
    from sailsoon.db import session_scope
    from sailsoon.models import HourlyForecast

    with session_scope() as s:
        rows = s.execute(
            select(HourlyForecast).order_by(HourlyForecast.t)
        ).scalars().all()
        assert rows[0].wind_speed_kt == 10.0
        assert rows[0].wave_height_m == 0.3
        assert rows[1].wind_gust_kt == 16.0


@respx.mock
def test_marine_ingester_extracts_hazards():
    from sailsoon.ingest.marine import BASE_URL, ingest_marine

    payload = {
        "properties": {
            "periods": [
                {
                    "name": "Tonight",
                    "startTime": "2026-04-20T18:00:00+00:00",
                    "endTime": "2026-04-21T06:00:00+00:00",
                    "detailedForecast": "Small Craft Advisory. NE winds 20 to 25 kt.",
                },
                {
                    "name": "Tuesday",
                    "startTime": "2026-04-21T06:00:00+00:00",
                    "endTime": "2026-04-21T18:00:00+00:00",
                    "detailedForecast": "N winds 10 kt. Seas 2 ft.",
                },
            ]
        }
    }
    respx.get(BASE_URL.format(zone="ANZ330")).mock(return_value=Response(200, json=payload))
    # Only one of our locations uses ANZ330 in this test run — that's fine; the others will also hit the same mock since multiple locations share ANZ330/ANZ335. Mock both.
    respx.get(BASE_URL.format(zone="ANZ335")).mock(return_value=Response(200, json=payload))
    respx.get(BASE_URL.format(zone="ANZ350")).mock(return_value=Response(200, json=payload))

    counts = ingest_marine(location_ids=["kings_point"])
    assert counts["kings_point"] == 2

    from sqlalchemy import select
    from sailsoon.db import session_scope
    from sailsoon.models import MarineForecast

    with session_scope() as s:
        rows = s.execute(select(MarineForecast).order_by(MarineForecast.valid_from)).scalars().all()
        assert "Small Craft Advisory" in rows[0].hazards
        assert rows[1].hazards == []
