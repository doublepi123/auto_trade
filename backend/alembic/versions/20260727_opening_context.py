"""add causal opening-context telemetry

Revision ID: 20260727_opening_context
Revises: 20260726_opening_stop
Create Date: 2026-07-27 07:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260727_opening_context"
down_revision: Union[str, Sequence[str], None] = (
    "20260726_opening_stop"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    "candidate_overnight_gap_bps",
    "candidate_prev_close_to_signal_bps",
    "benchmark_qqq_return_bps",
    "benchmark_dia_return_bps",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "opening_momentum_shadow_runs",
            sa.Column(name, sa.Float(), nullable=True),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("opening_momentum_shadow_runs", name)
