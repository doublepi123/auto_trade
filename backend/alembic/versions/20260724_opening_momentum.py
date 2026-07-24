"""add prospective opening-momentum shadow evidence

Revision ID: 20260724_opening_momentum
Revises: 20260522_add_llm_interactions
Create Date: 2026-07-24 09:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260724_opening_momentum"
down_revision: Union[str, Sequence[str], None] = (
    "20260522_add_llm_interactions"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opening_momentum_shadow_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=100), nullable=False),
        sa.Column("config_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("signal_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selection_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "universe_source",
            sa.String(length=32),
            nullable=False,
            server_default="",
        ),
        sa.Column("universe_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("universe_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "excluded_symbols_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("ranking_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("candidate_symbol", sa.String(length=50), nullable=True),
        sa.Column("market_return_bps", sa.Float(), nullable=True),
        sa.Column("candidate_return_bps", sa.Float(), nullable=True),
        sa.Column("excess_return_bps", sa.Float(), nullable=True),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("exit_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("gross_return_bps", sa.Float(), nullable=True),
        sa.Column("estimated_cost_bps", sa.Float(), nullable=False),
        sa.Column("net_return_bps", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_date",
            "config_version",
            name="uq_opening_momentum_shadow_session_version",
        ),
    )
    op.create_index(
        "ix_opening_momentum_shadow_status_session",
        "opening_momentum_shadow_runs",
        ["status", "session_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_opening_momentum_shadow_status_session",
        table_name="opening_momentum_shadow_runs",
    )
    op.drop_table("opening_momentum_shadow_runs")
