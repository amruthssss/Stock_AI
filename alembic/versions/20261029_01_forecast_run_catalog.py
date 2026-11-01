"""Create the forecast run catalog and per-model summary metrics."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261029_01"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "forecast_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("generated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("m5_ingestion_run_id", sa.String(length=64), nullable=False),
        sa.Column("state_id", sa.String(length=8), nullable=False),
        sa.Column("category_id", sa.String(length=32), nullable=False),
        sa.Column("series_count", sa.Integer(), nullable=False),
        sa.Column("total_matching_series", sa.Integer(), nullable=True),
        sa.Column("series_limit", sa.Integer(), nullable=True),
        sa.Column("selection_method", sa.Text(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.CheckConstraint("status = 'completed'", name="ck_forecast_runs_status"),
        sa.CheckConstraint("schema_version > 0", name="ck_forecast_runs_schema_version"),
        sa.CheckConstraint("series_count > 0", name="ck_forecast_runs_series_count"),
        sa.CheckConstraint(
            "total_matching_series IS NULL OR total_matching_series >= series_count",
            name="ck_forecast_runs_total_series",
        ),
        sa.CheckConstraint(
            "series_limit IS NULL OR series_limit > 0", name="ck_forecast_runs_series_limit"
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_forecast_runs"),
    )
    op.create_index(
        "ix_forecast_runs_generated_at_utc",
        "forecast_runs",
        ["generated_at_utc"],
    )
    op.create_table(
        "forecast_metrics",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("evaluation_period", sa.String(length=20), nullable=False),
        sa.Column("model_name", sa.String(length=64), nullable=False),
        sa.Column("mae", sa.Float(), nullable=False),
        sa.Column("wape", sa.Float(), nullable=True),
        sa.Column("bias", sa.Float(), nullable=True),
        sa.Column("observations", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "evaluation_period IN ('rolling_origin', 'held_out')",
            name="ck_forecast_metrics_period",
        ),
        sa.CheckConstraint("mae >= 0", name="ck_forecast_metrics_mae"),
        sa.CheckConstraint("wape IS NULL OR wape >= 0", name="ck_forecast_metrics_wape"),
        sa.CheckConstraint("observations > 0", name="ck_forecast_metrics_observations"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["forecast_runs.run_id"], ondelete="CASCADE", name="fk_forecast_metrics_run"
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "evaluation_period", "model_name", name="pk_forecast_metrics"
        ),
    )
    op.create_index(
        "ix_forecast_metrics_period_model",
        "forecast_metrics",
        ["evaluation_period", "model_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_forecast_metrics_period_model", table_name="forecast_metrics")
    op.drop_table("forecast_metrics")
    op.drop_index("ix_forecast_runs_generated_at_utc", table_name="forecast_runs")
    op.drop_table("forecast_runs")
