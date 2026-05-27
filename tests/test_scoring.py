from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sailsoon.config import Rule, RuleSet
from sailsoon.scoring import HourReport, find_windows, _ceiling, _score_hour, _trapezoid


def _rule(**kw) -> Rule:
    return Rule(**kw)


def test_trapezoid_midband_is_one():
    r = _rule(type="trapezoid", source="wind_speed_kt", low_zero=5, low_full=8, high_full=18, high_zero=22)
    assert _trapezoid(12.0, r) == 1.0


def test_trapezoid_below_zero_is_zero():
    r = _rule(type="trapezoid", source="wind_speed_kt", low_zero=5, low_full=8, high_full=18, high_zero=22)
    assert _trapezoid(4.0, r) == 0.0
    assert _trapezoid(23.0, r) == 0.0


def test_trapezoid_ramps():
    r = _rule(type="trapezoid", source="wind_speed_kt", low_zero=5, low_full=8, high_full=18, high_zero=22)
    # Halfway up the low ramp (5->8) at 6.5
    assert abs(_trapezoid(6.5, r) - 0.5) < 1e-6
    # Halfway down the high ramp (18->22) at 20
    assert abs(_trapezoid(20.0, r) - 0.5) < 1e-6


def test_ceiling_monotonic():
    r = _rule(type="ceiling", source="wind_gust_kt", warn_at=20, fail_at=28)
    assert _ceiling(15.0, r) == 1.0
    assert _ceiling(24.0, r) == 0.5
    assert _ceiling(30.0, r) == 0.0


def test_ceiling_none_is_neutral():
    r = _rule(type="ceiling", source="wave_height_m", warn_at=0.8, fail_at=1.5)
    assert _ceiling(None, r) == 0.75


def test_air_temperature_rule_uses_actual_hourly_temperature():
    rules = RuleSet(
        rules={
            "air_temp": _rule(
                type="trapezoid",
                source="air_temp_c",
                low_zero=0,
                low_full=10,
                high_full=28,
                high_zero=38,
                weight=1.0,
            )
        }
    )
    cold = SimpleNamespace(
        wind_speed_kt=None,
        wind_gust_kt=None,
        wave_height_m=None,
        precip_prob_pct=None,
        air_temp_c=-1.0,
        cloud_cover_pct=None,
    )
    comfortable = SimpleNamespace(
        wind_speed_kt=None,
        wind_gust_kt=None,
        wave_height_m=None,
        precip_prob_pct=None,
        air_temp_c=18.0,
        cloud_cover_pct=None,
    )

    cold_score, cold_reasons = _score_hour(cold, [], rules)
    comfortable_score, comfortable_reasons = _score_hour(comfortable, [], rules)

    assert cold_score == 0.0
    assert cold_reasons["air_temp"] == 0.0
    assert comfortable_score == 1.0
    assert comfortable_reasons["air_temp"] == 1.0


def _hr(t: datetime, score: float, daylight: bool = True) -> HourReport:
    return HourReport(t=t, score=score, label="", daylight=daylight, reasons={}, hazards=[], raw={})


def test_find_windows_minimum_duration():
    rules = RuleSet(min_score=0.6, min_duration_hours=3)
    start = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
    reports = [
        _hr(start + timedelta(hours=0), 0.8),
        _hr(start + timedelta(hours=1), 0.7),  # 2 consecutive good — below min_duration
        _hr(start + timedelta(hours=2), 0.3),
        _hr(start + timedelta(hours=3), 0.9),
        _hr(start + timedelta(hours=4), 0.8),
        _hr(start + timedelta(hours=5), 0.7),  # 3 consecutive good — counts
    ]
    windows = find_windows(reports, rules)
    assert len(windows) == 1
    assert windows[0].start == start + timedelta(hours=3)
    assert windows[0].end == start + timedelta(hours=6)
    assert windows[0].min_score == 0.7


def test_find_windows_skips_night():
    rules = RuleSet(min_score=0.6, min_duration_hours=2)
    start = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
    reports = [
        _hr(start + timedelta(hours=0), 0.9, daylight=True),
        _hr(start + timedelta(hours=1), 0.9, daylight=False),  # night kills the run
        _hr(start + timedelta(hours=2), 0.9, daylight=True),
        _hr(start + timedelta(hours=3), 0.9, daylight=True),
    ]
    windows = find_windows(reports, rules)
    assert len(windows) == 1
    assert windows[0].start == start + timedelta(hours=2)
