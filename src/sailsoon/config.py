from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SAILSOON_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./sailsoon.db"
    locations_file: Path = CONFIG_DIR / "locations.yml"
    rules_file: Path = CONFIG_DIR / "rules.yml"
    http_user_agent: str = "sail-soon/0.1 (+https://github.com/kwhbitpro/sail-soon)"
    forecast_days: int = 7


class Location(BaseModel):
    id: str
    name: str
    tide_station: str
    marine_zone: str
    latitude: float
    longitude: float
    timezone: str = "America/New_York"


class DaylightWindow(BaseModel):
    sunrise_buffer_minutes: int = 30
    sunset_buffer_minutes: int = -30


class Rule(BaseModel):
    type: str
    source: str
    weight: float = 1.0
    # Trapezoid
    low_zero: float | None = None
    low_full: float | None = None
    high_full: float | None = None
    high_zero: float | None = None
    # Ceiling
    warn_at: float | None = None
    fail_at: float | None = None
    # NWS hazard
    fail_on: list[str] = Field(default_factory=list)


class RuleSet(BaseModel):
    profile: str = "default_daysail"
    daylight_window: DaylightWindow = Field(default_factory=DaylightWindow)
    min_score: float = 0.6
    min_duration_hours: int = 3
    rules: dict[str, Rule] = Field(default_factory=dict)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def load_locations() -> list[Location]:
    data = _load_yaml(get_settings().locations_file)
    return [Location(**loc) for loc in data["locations"]]


def get_location(location_id: str) -> Location:
    for loc in load_locations():
        if loc.id == location_id:
            return loc
    raise KeyError(f"Unknown location: {location_id}")


@lru_cache(maxsize=1)
def load_rules() -> RuleSet:
    return RuleSet(**_load_yaml(get_settings().rules_file))
