"""persist the position-write decision and cost-basis opening time

Revision ID: 20260922_fill_intent
Revises: 20260921_fill_settlements
Create Date: 2026-09-22 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260922_fill_intent"
down_revision: str | Sequence[str] | None = "20260921_fill_settlements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Even positive remaining quantity does not prove a position write occurred.
    # False conservatively prevents replaying an unproven write; NULL preserves
    # the unknown opening time instead of inventing it from receipt creation.
    op.add_column("fill_settlements", sa.Column(
        "persist_position", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column("fill_settlements", sa.Column(
        "cost_basis_opened_at", sa.DateTime(timezone=True), nullable=True,
    ))


def downgrade() -> None:
    op.drop_column("fill_settlements", "cost_basis_opened_at")
    op.drop_column("fill_settlements", "persist_position")
