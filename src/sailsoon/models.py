from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    tide_station: Mapped[str] = mapped_column(String(32))
    marine_zone: Mapped[str] = mapped_column(String(16))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(64))

    tides: Mapped[list["TidePrediction"]] = relationship(back_populates="location")
    marine_forecasts: Mapped[list["MarineForecast"]] = relationship(back_populates="location")
    hourly: Mapped[list["HourlyForecast"]] = relationship(back_populates="location")


class TidePrediction(Base):
    __tablename__ = "tide_predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"))
    # `t` is the predicted time (UTC). `type` is "H"/"L" for high/low extremes
    # when using hilo product; null for hourly predictions.
    t: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    height_m: Mapped[float] = mapped_column(Float)
    type: Mapped[str | None] = mapped_column(String(2), nullable=True)

    location: Mapped[Location] = relationship(back_populates="tides")

    __table_args__ = (
        UniqueConstraint("location_id", "t", "type", name="uq_tide_pred_location_time_type"),
        Index("ix_tide_pred_location_t", "location_id", "t"),
    )


class MarineForecast(Base):
    __tablename__ = "marine_forecasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"))
    zone: Mapped[str] = mapped_column(String(16))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    headline: Mapped[str | None] = mapped_column(String(512), nullable=True)
    hazards: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw_text: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    location: Mapped[Location] = relationship(back_populates="marine_forecasts")

    __table_args__ = (
        Index("ix_marine_forecast_zone_time", "zone", "valid_from", "valid_to"),
    )


class HourlyForecast(Base):
    """Hourly consolidated wind/wave/precip from Open-Meteo, keyed by location."""

    __tablename__ = "hourly_forecasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"))
    t: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    wind_speed_kt: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_gust_kt: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_dir_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    wave_height_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    swell_period_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    precip_prob_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_cover_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    location: Mapped[Location] = relationship(back_populates="hourly")

    __table_args__ = (
        UniqueConstraint("location_id", "t", name="uq_hourly_location_time"),
        Index("ix_hourly_location_t", "location_id", "t"),
    )


class Profile(Base):
    """User-editable filter preset. No login; mutations are gated by a token
    returned at creation time."""

    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug
    name: Mapped[str] = mapped_column(String(255))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    token_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
