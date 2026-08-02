"""add durable cross-process job leases

Revision ID: 20260801_durable_job_leases
Revises: 20260801_watchlist_quant_v6
Create Date: 2026-08-01 12:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260801_durable_job_leases"
down_revision: Union[str, Sequence[str], None] = (
    "20260801_watchlist_quant_v6"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NO_DELETE_TRIGGER = "trg_durable_job_leases_no_delete"


def upgrade() -> None:
    op.create_table(
        "durable_job_leases",
        sa.Column("lease_key", sa.String(length=128), nullable=False),
        sa.Column("holder_id", sa.String(length=128), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("acquired_at_epoch_ms", sa.Integer(), nullable=False),
        sa.Column("renewed_at_epoch_ms", sa.Integer(), nullable=False),
        sa.Column("expires_at_epoch_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "length(lease_key) > 0 AND length(lease_key) <= 128 "
            "AND lease_key = trim(lease_key)",
            name="ck_durable_job_lease_key",
        ),
        sa.CheckConstraint(
            "length(holder_id) > 0 AND length(holder_id) <= 128 "
            "AND holder_id = trim(holder_id)",
            name="ck_durable_job_lease_holder",
        ),
        sa.CheckConstraint(
            "fencing_token >= 1",
            name="ck_durable_job_lease_fencing_token",
        ),
        sa.CheckConstraint(
            "acquired_at_epoch_ms >= 0 "
            "AND renewed_at_epoch_ms >= 0 "
            "AND expires_at_epoch_ms >= 0",
            name="ck_durable_job_lease_epoch_ms",
        ),
        sa.PrimaryKeyConstraint("lease_key"),
    )
    op.execute(
        f"CREATE TRIGGER {_NO_DELETE_TRIGGER} "
        "BEFORE DELETE ON durable_job_leases "
        "BEGIN "
        "SELECT RAISE(ABORT, 'durable_job_leases rows cannot be deleted'); "
        "END"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_NO_DELETE_TRIGGER}")
    op.drop_table("durable_job_leases")
