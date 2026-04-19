"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-19
"""
from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("tide_station", sa.String(32), nullable=False),
        sa.Column("marine_zone", sa.String(16), nullable=False),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
    )

    op.create_table(
        "tide_predictions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "location_id",
            sa.String(64),
            sa.ForeignKey("locations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("t", sa.DateTime(timezone=True), nullable=False),
        sa.Column("height_m", sa.Float, nullable=False),
        sa.Column("type", sa.String(2), nullable=True),
        sa.UniqueConstraint("location_id", "t", "type", name="uq_tide_pred_location_time_type"),
    )
    op.create_index("ix_tide_pred_location_t", "tide_predictions", ["location_id", "t"])

    op.create_table(
        "marine_forecasts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "location_id",
            sa.String(64),
            sa.ForeignKey("locations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("zone", sa.String(16), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("headline", sa.String(512), nullable=True),
        sa.Column("hazards", sa.JSON, nullable=False),
        sa.Column("raw_text", sa.Text, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_marine_forecast_zone_time",
        "marine_forecasts",
        ["zone", "valid_from", "valid_to"],
    )

    op.create_table(
        "hourly_forecasts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "location_id",
            sa.String(64),
            sa.ForeignKey("locations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("t", sa.DateTime(timezone=True), nullable=False),
        sa.Column("wind_speed_kt", sa.Float, nullable=True),
        sa.Column("wind_gust_kt", sa.Float, nullable=True),
        sa.Column("wind_dir_deg", sa.Float, nullable=True),
        sa.Column("wave_height_m", sa.Float, nullable=True),
        sa.Column("swell_period_s", sa.Float, nullable=True),
        sa.Column("precip_prob_pct", sa.Float, nullable=True),
        sa.Column("air_temp_c", sa.Float, nullable=True),
        sa.Column("cloud_cover_pct", sa.Float, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("location_id", "t", name="uq_hourly_location_time"),
    )
    op.create_index("ix_hourly_location_t", "hourly_forecasts", ["location_id", "t"])


def downgrade() -> None:
    op.drop_index("ix_hourly_location_t", table_name="hourly_forecasts")
    op.drop_table("hourly_forecasts")
    op.drop_index("ix_marine_forecast_zone_time", table_name="marine_forecasts")
    op.drop_table("marine_forecasts")
    op.drop_index("ix_tide_pred_location_t", table_name="tide_predictions")
    op.drop_table("tide_predictions")
    op.drop_table("locations")
