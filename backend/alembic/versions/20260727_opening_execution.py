"""add crash-safe opening-momentum execution journal

Revision ID: 20260727_opening_execution
Revises: 20260727_opening_context
Create Date: 2026-07-27 14:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260727_opening_execution"
down_revision: Union[str, Sequence[str], None] = (
    "20260727_opening_context"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opening_momentum_executions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=160), nullable=False),
        sa.Column("config_version", sa.String(length=64), nullable=False),
        sa.Column("universe_source", sa.String(length=48), nullable=False),
        sa.Column("selection_run_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("symbol", sa.String(length=50), nullable=True),
        sa.Column("signal_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("armed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("universe_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("market_return_bps", sa.Float(), nullable=True),
        sa.Column("candidate_return_bps", sa.Float(), nullable=True),
        sa.Column("excess_return_bps", sa.Float(), nullable=True),
        sa.Column("reference_entry_price", sa.Float(), nullable=True),
        sa.Column("max_price_deviation_bps", sa.Float(), nullable=False),
        sa.Column("stop_loss_pct", sa.Float(), nullable=False),
        sa.Column("max_holding_minutes", sa.Integer(), nullable=False),
        sa.Column("signal_context_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("submit_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entry_order_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("exit_order_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("entry_filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("exit_filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("net_pnl", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_date",
            name="uq_opening_momentum_execution_session",
        ),
    )
    op.create_index(
        "ix_opening_momentum_execution_status_session",
        "opening_momentum_executions",
        ["status", "session_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_opening_momentum_execution_status_session",
        table_name="opening_momentum_executions",
    )
    op.drop_table("opening_momentum_executions")
