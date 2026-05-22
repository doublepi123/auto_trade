"""add minimum profit amount to strategy_config

Revision ID: 20260522_add_min_profit_amount
Revises: 20260520_add_llm_interval_minutes
Create Date: 2026-05-22 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260522_add_min_profit_amount"
down_revision: Union[str, Sequence[str], None] = "20260520_add_llm_interval_minutes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "strategy_config",
        sa.Column("min_profit_amount", sa.Float(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("strategy_config", "min_profit_amount")
