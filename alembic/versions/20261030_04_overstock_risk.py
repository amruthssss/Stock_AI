"""Persist transparent overstock targets and excess supply units."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_04"
down_revision: Union[str, None] = "20261030_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_risk_runs",
        sa.Column("overstock_review_period_days", sa.Integer(), nullable=False, server_default="28"),
    )
    op.add_column(
        "inventory_risk_items",
        sa.Column("overstock_target_units", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "inventory_risk_items",
        sa.Column("overstock_units", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        """
        UPDATE inventory_risk_items
        SET overstock_target_units = reorder_point_units + CEIL(ROUND(forecast_average_daily_units::numeric, 4) * 28),
            overstock_units = GREATEST(
                available_supply_at_lead_time
                - (reorder_point_units + CEIL(ROUND(forecast_average_daily_units::numeric, 4) * 28)),
                0
            )
        """
    )
    op.create_check_constraint(
        "ck_inventory_risk_items_overstock_target",
        "inventory_risk_items",
        "overstock_target_units >= 0",
    )
    op.create_check_constraint(
        "ck_inventory_risk_items_overstock_units",
        "inventory_risk_items",
        "overstock_units >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_inventory_risk_items_overstock_units", "inventory_risk_items", type_="check"
    )
    op.drop_constraint(
        "ck_inventory_risk_items_overstock_target", "inventory_risk_items", type_="check"
    )
    op.drop_column("inventory_risk_items", "overstock_units")
    op.drop_column("inventory_risk_items", "overstock_target_units")
    op.drop_column("inventory_risk_runs", "overstock_review_period_days")
