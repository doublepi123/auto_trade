"""add stop-aware opening-momentum evidence

Revision ID: 20260726_opening_stop
Revises: 20260724_opening_momentum
Create Date: 2026-07-26 21:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260726_opening_stop"
down_revision: Union[str, Sequence[str], None] = (
    "20260724_opening_momentum"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opening_momentum_shadow_runs",
        sa.Column("stop_loss_pct", sa.Float(), nullable=True),
    )
    op.add_column(
        "opening_momentum_shadow_runs",
        sa.Column(
            "maximum_adverse_excursion_bps",
            sa.Float(),
            nullable=True,
        ),
    )
    op.add_column(
        "opening_momentum_shadow_runs",
        sa.Column(
            "maximum_favorable_excursion_bps",
            sa.Float(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "opening_momentum_shadow_runs",
        "maximum_favorable_excursion_bps",
    )
    op.drop_column(
        "opening_momentum_shadow_runs",
        "maximum_adverse_excursion_bps",
    )
    op.drop_column("opening_momentum_shadow_runs", "stop_loss_pct")
