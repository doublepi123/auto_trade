"""add durable fill accounting receipts

Revision ID: 20260921_fill_settlements
Revises: 20260802_opening_breakout_depth
Create Date: 2026-09-21 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260921_fill_settlements"
down_revision: str | Sequence[str] | None = "20260802_opening_breakout_depth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fill_settlements",
        sa.Column("broker_order_id", sa.Text(), nullable=False, primary_key=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("booked_quantity", sa.Float(), nullable=False),
        sa.Column("booked_price", sa.Float(), nullable=False),
        sa.Column("quantity_source", sa.Text(), nullable=False),
        sa.Column("price_source", sa.Text(), nullable=False),
        sa.Column("cost_basis_price", sa.Float(), nullable=True),
        sa.Column("consumed_quantity", sa.Float(), nullable=True),
        sa.Column("gross_pnl", sa.Float(), nullable=True),
        sa.Column("net_pnl", sa.Float(), nullable=True),
        sa.Column("pnl_source", sa.Text(), nullable=True),
        sa.Column("tracked_side", sa.Text(), nullable=True),
        sa.Column("tracked_quantity_after", sa.Float(), nullable=False),
        sa.Column("tracked_cost_after", sa.Float(), nullable=False),
        sa.Column("first_terminal_status", sa.Text(), nullable=False),
        sa.Column("risk_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("risk_applied_via", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_fill_settlements_no_delete "
        "BEFORE DELETE ON fill_settlements "
        "BEGIN SELECT RAISE(ABORT, 'fill_settlements rows cannot be deleted'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_fill_settlements_no_delete")
    op.drop_table("fill_settlements")
