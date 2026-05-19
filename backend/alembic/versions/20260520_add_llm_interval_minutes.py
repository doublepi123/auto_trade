"""add llm interval minutes to strategy_config

Revision ID: 20260520_add_llm_interval_minutes
Revises: 20260602_add_llm_interval_fields
Create Date: 2026-05-20 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260520_add_llm_interval_minutes"
down_revision: Union[str, Sequence[str], None] = "20260602_add_llm_interval_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "strategy_config",
        sa.Column("llm_interval_minutes", sa.Integer(), server_default="240", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("strategy_config", "llm_interval_minutes")
