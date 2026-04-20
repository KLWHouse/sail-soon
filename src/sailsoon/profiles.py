"""Shareable filter presets ("profiles") so friends can save their own
preferred locations and rule thresholds and get a personal ICS URL.

No login: creating a profile returns a one-time edit token. Updates and
deletes require that token. The token is hashed in the DB.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import DaylightWindow, Rule, RuleSet, load_rules
from .models import Profile as ProfileModel


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}[a-z0-9]$")


class ProfileConfig(BaseModel):
    """Profile payload. All fields optional — absent keys fall back to the
    base `config/rules.yml` values."""

    locations: list[str] = Field(default_factory=list)
    min_score: float | None = None
    min_duration_hours: int | None = None
    daylight_window: DaylightWindow | None = None
    # Partial rule overrides: {"wind_speed": {"low_full": 10}, ...}
    rule_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Calendar output defaults
    min_verdict: str | None = None  # "maybe" | "go"
    include_tides: bool = False


class ProfileIn(BaseModel):
    id: str
    name: str
    config: ProfileConfig = Field(default_factory=ProfileConfig)

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("id must be kebab-case, 4-64 chars, [a-z0-9-]")
        return v


class ProfileUpdate(BaseModel):
    name: str | None = None
    config: ProfileConfig | None = None


class ProfileOut(BaseModel):
    id: str
    name: str
    config: ProfileConfig
    created_at: datetime
    updated_at: datetime


class ProfileCreated(ProfileOut):
    edit_token: str  # shown once; store it somewhere safe


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _verify_token(token: str, hash_: str) -> bool:
    return secrets.compare_digest(_hash_token(token), hash_)


def _to_out(row: ProfileModel) -> ProfileOut:
    return ProfileOut(
        id=row.id,
        name=row.name,
        config=ProfileConfig(**(row.config or {})),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def create_profile(session: Session, payload: ProfileIn) -> ProfileCreated:
    existing = session.get(ProfileModel, payload.id)
    if existing is not None:
        raise ValueError(f"profile '{payload.id}' already exists")
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    row = ProfileModel(
        id=payload.id,
        name=payload.name,
        config=payload.config.model_dump(),
        token_hash=_hash_token(token),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    out = _to_out(row)
    return ProfileCreated(**out.model_dump(), edit_token=token)


def get_profile(session: Session, profile_id: str) -> ProfileModel | None:
    return session.get(ProfileModel, profile_id)


def list_profiles(session: Session) -> list[ProfileOut]:
    rows = session.execute(select(ProfileModel).order_by(ProfileModel.id)).scalars().all()
    return [_to_out(r) for r in rows]


def update_profile(
    session: Session, profile_id: str, token: str, patch: ProfileUpdate
) -> ProfileOut:
    row = get_profile(session, profile_id)
    if row is None:
        raise KeyError(profile_id)
    if not _verify_token(token, row.token_hash):
        raise PermissionError("invalid edit token")
    if patch.name is not None:
        row.name = patch.name
    if patch.config is not None:
        row.config = patch.config.model_dump()
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    return _to_out(row)


def delete_profile(session: Session, profile_id: str, token: str) -> None:
    row = get_profile(session, profile_id)
    if row is None:
        raise KeyError(profile_id)
    if not _verify_token(token, row.token_hash):
        raise PermissionError("invalid edit token")
    session.delete(row)


def effective_ruleset(profile_cfg: ProfileConfig | None, base: RuleSet | None = None) -> RuleSet:
    """Merge a profile's overrides on top of the base rules.yml."""
    base = base or load_rules()
    if profile_cfg is None:
        return base
    merged_rules: dict[str, Rule] = {}
    for name, rule in base.rules.items():
        override = profile_cfg.rule_overrides.get(name) or {}
        if override:
            data = rule.model_dump()
            data.update(override)
            merged_rules[name] = Rule(**data)
        else:
            merged_rules[name] = rule
    return RuleSet(
        profile=base.profile,
        daylight_window=profile_cfg.daylight_window or base.daylight_window,
        min_score=profile_cfg.min_score if profile_cfg.min_score is not None else base.min_score,
        min_duration_hours=(
            profile_cfg.min_duration_hours
            if profile_cfg.min_duration_hours is not None
            else base.min_duration_hours
        ),
        rules=merged_rules,
    )
