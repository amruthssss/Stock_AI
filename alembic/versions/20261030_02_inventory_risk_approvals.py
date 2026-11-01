"""Add append-only human review history for saved risk recommendations."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_02"
down_revision: Union[str, None] = "20261030_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_risk_approvals",
        sa.Column("approval_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("risk_run_id", sa.String(length=64), nullable=False),
        sa.Column("series_id", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recommended_order_quantity_units", sa.Integer(), nullable=False),
        sa.Column("stockout_probability", sa.Float(), nullable=False),
        sa.Column("inventory_provenance", sa.String(length=100), nullable=False),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'revision_requested')",
            name="ck_inventory_risk_approvals_decision",
        ),
        sa.CheckConstraint(
            "recommended_order_quantity_units > 0",
            name="ck_inventory_risk_approvals_order_quantity",
        ),
        sa.CheckConstraint(
            "stockout_probability >= 0 AND stockout_probability <= 1",
            name="ck_inventory_risk_approvals_probability",
        ),
        sa.ForeignKeyConstraint(
            ["risk_run_id", "series_id"],
            ["inventory_risk_items.run_id", "inventory_risk_items.series_id"],
            name="fk_inventory_risk_approvals_source_item",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("approval_id", name="pk_inventory_risk_approvals"),
        sa.UniqueConstraint("idempotency_key", name="uq_inventory_risk_approvals_idempotency_key"),
    )
    op.create_index(
        "ix_inventory_risk_approvals_created_at_utc",
        "inventory_risk_approvals",
        ["created_at_utc"],
    )
    op.create_index(
        "ix_inventory_risk_approvals_risk_run_id",
        "inventory_risk_approvals",
        ["risk_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_risk_approvals_risk_run_id", table_name="inventory_risk_approvals")
    op.drop_index("ix_inventory_risk_approvals_created_at_utc", table_name="inventory_risk_approvals")
    op.drop_table("inventory_risk_approvals")
