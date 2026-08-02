"""add opening-range breakout depth evidence

Revision ID: 20260802_opening_breakout_depth
Revises: 20260801_durable_job_leases
Create Date: 2026-08-02 14:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260802_opening_breakout_depth"
down_revision: Union[str, Sequence[str], None] = (
    "20260801_durable_job_leases"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "opening_momentum_shadow_runs"
_COLUMN = "candidate_breakout_depth_bps"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
