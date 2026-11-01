"""Create persistence tables for synthetic inventory-risk reports."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261029_02"
down_revision: Union[str, None] = "20261029_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_risk_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("generated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("simulation_run_id", sa.String(length=64), nullable=False),
        sa.Column("state_id", sa.String(length=8), nullable=False),
        sa.Column("category_id", sa.String(length=32), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("series_count", sa.Integer(), nullable=False),
        sa.Column("total_matching_series", sa.Integer(), nullable=True),
        sa.Column("series_limit", sa.Integer(), nullable=True),
        sa.Column("selection_method", sa.Text(), nullable=False),
        sa.Column("service_level", sa.Float(), nullable=False),
        sa.Column("forecast_model", sa.String(length=64), nullable=False),
        sa.Column("forecast_horizon_description", sa.Text(), nullable=False),
        sa.Column("inventory_provenance", sa.String(length=100), nullable=False),
        sa.Column("demand_reference", sa.Text(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.CheckConstraint("status = 'completed'", name="ck_inventory_risk_runs_status"),
        sa.CheckConstraint("schema_version > 0", name="ck_inventory_risk_runs_schema_version"),
        sa.CheckConstraint("service_level > 0 AND service_level < 1", name="ck_inventory_risk_runs_service_level"),
        sa.CheckConstraint("series_count > 0", name="ck_inventory_risk_runs_series_count"),
        sa.CheckConstraint(
            "total_matching_series IS NULL OR total_matching_series >= series_count",
            name="ck_inventory_risk_runs_total_series",
        ),
        sa.CheckConstraint(
            "series_limit IS NULL OR series_limit > 0", name="ck_inventory_risk_runs_series_limit"
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_inventory_risk_runs"),
    )
    op.create_index(
        "ix_inventory_risk_runs_generated_at_utc",
        "inventory_risk_runs",
        ["generated_at_utc"],
    )
    op.create_table(
        "inventory_risk_items",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("series_id", sa.String(length=128), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("service_level", sa.Float(), nullable=False),
        sa.Column("forecast_days_available", sa.Integer(), nullable=False),
        sa.Column("forecast_average_daily_units", sa.Float(), nullable=False),
        sa.Column("stock_coverage_days", sa.Float(), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=False),
        sa.Column("expected_lead_time_demand_units", sa.Float(), nullable=False),
        sa.Column("lead_time_demand_std_units", sa.Float(), nullable=False),
        sa.Column("lead_time_demand_quantile_units", sa.Float(), nullable=False),
        sa.Column("safety_stock_units", sa.Float(), nullable=False),
        sa.Column("reorder_point_units", sa.Integer(), nullable=False),
        sa.Column("incoming_units_within_lead_time", sa.Integer(), nullable=False),
        sa.Column("available_supply_at_lead_time", sa.Integer(), nullable=False),
        sa.Column("stockout_probability", sa.Float(), nullable=False),
        sa.Column("stockout_exposure_units", sa.Integer(), nullable=False),
        sa.Column("suggested_order_quantity_units", sa.Integer(), nullable=False),
        sa.Column("expiry_units_at_risk", sa.Float(), nullable=False),
        sa.Column("expiry_value_at_risk_proxy", sa.Float(), nullable=False),
        sa.CheckConstraint("service_level > 0 AND service_level < 1", name="ck_inventory_risk_items_service_level"),
        sa.CheckConstraint("forecast_days_available > 0", name="ck_inventory_risk_items_forecast_days"),
        sa.CheckConstraint("lead_time_days > 0", name="ck_inventory_risk_items_lead_time"),
        sa.CheckConstraint("stockout_probability >= 0 AND stockout_probability <= 1", name="ck_inventory_risk_items_probability"),
        sa.CheckConstraint("reorder_point_units >= 0", name="ck_inventory_risk_items_reorder_point"),
        sa.CheckConstraint("suggested_order_quantity_units >= 0", name="ck_inventory_risk_items_order_quantity"),
        sa.CheckConstraint("expiry_units_at_risk >= 0", name="ck_inventory_risk_items_expiry_units"),
        sa.CheckConstraint("expiry_value_at_risk_proxy >= 0", name="ck_inventory_risk_items_expiry_value"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["inventory_risk_runs.run_id"], ondelete="CASCADE", name="fk_inventory_risk_items_run"
        ),
        sa.PrimaryKeyConstraint("run_id", "series_id", name="pk_inventory_risk_items"),
    )
    op.create_index(
        "ix_inventory_risk_items_probability",
        "inventory_risk_items",
        ["stockout_probability"],
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_risk_items_probability", table_name="inventory_risk_items")
    op.drop_table("inventory_risk_items")
    op.drop_index("ix_inventory_risk_runs_generated_at_utc", table_name="inventory_risk_runs")
    op.drop_table("inventory_risk_runs")
