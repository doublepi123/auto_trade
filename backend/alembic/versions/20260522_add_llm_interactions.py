"""add llm interaction history

Revision ID: 20260522_add_llm_interactions
Revises: 20260522_auto_resume_pause
Create Date: 2026-05-22 11:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260522_add_llm_interactions"
down_revision: Union[str, Sequence[str], None] = "20260522_auto_resume_pause"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_interactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("interaction_type", sa.String(length=20), nullable=False, server_default="analyze"),
        sa.Column("symbol", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("market", sa.String(length=10), nullable=False, server_default="US"),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("raw_response", sa.Text(), nullable=False, server_default=""),
        sa.Column("parsed_response", sa.Text(), nullable=False, server_default=""),
        sa.Column("context_snapshot", sa.Text(), nullable=False, server_default=""),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("order_action", sa.String(length=30), nullable=False, server_default="NONE"),
        sa.Column("order_status", sa.String(length=30), nullable=True),
        sa.Column("order_id", sa.String(length=100), nullable=True),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_interactions_created_at", "llm_interactions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_interactions_created_at", table_name="llm_interactions")
    op.drop_table("llm_interactions")
