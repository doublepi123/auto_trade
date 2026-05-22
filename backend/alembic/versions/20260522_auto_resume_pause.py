"""add pause auto resume configuration

Revision ID: 20260522_auto_resume_pause
Revises: 20260522_add_min_profit_amount
Create Date: 2026-05-22 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260522_auto_resume_pause"
down_revision: Union[str, Sequence[str], None] = "20260522_add_min_profit_amount"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "strategy_config",
        sa.Column("auto_resume_minutes", sa.Integer(), server_default="3", nullable=False),
    )
    op.add_column(
        "runtime_state",
        sa.Column("pause_reason", sa.Text(), server_default="", nullable=False),
    )
    op.add_column("runtime_state", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "runtime_state",
        sa.Column("pause_auto_resumable", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("UPDATE strategy_config SET llm_interval_minutes = 2 WHERE llm_interval_minutes = 240")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE strategy_config SET llm_interval_minutes = 240 WHERE llm_interval_minutes = 2")
    op.drop_column("runtime_state", "pause_auto_resumable")
    op.drop_column("runtime_state", "paused_at")
    op.drop_column("runtime_state", "pause_reason")
    op.drop_column("strategy_config", "auto_resume_minutes")
