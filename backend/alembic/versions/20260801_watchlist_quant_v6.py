"""add immutable watchlist quant-v6 publication storage

Revision ID: 20260801_watchlist_quant_v6
Revises: 20260727_opening_execution
Create Date: 2026-08-01 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260801_watchlist_quant_v6"
down_revision: Union[str, Sequence[str], None] = (
    "20260727_opening_execution"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = (
    "watchlist_quant_v6_registrations",
    "watchlist_quant_v6_artifacts",
    "watchlist_quant_v6_publications",
    "watchlist_quant_v6_publication_artifacts",
)


_DUPLICATE_PREDICATES = {
    "watchlist_quant_v6_registrations": (
        "(NEW.id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM watchlist_quant_v6_registrations "
        "WHERE id = NEW.id)) "
        "OR EXISTS (SELECT 1 FROM watchlist_quant_v6_registrations "
        "WHERE identity_sha256 = NEW.identity_sha256) "
        "OR (NEW.id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM watchlist_quant_v6_registrations "
        "WHERE id = NEW.id "
        "AND identity_sha256 = NEW.identity_sha256))"
    ),
    "watchlist_quant_v6_artifacts": (
        "EXISTS (SELECT 1 FROM watchlist_quant_v6_artifacts "
        "WHERE digest_sha256 = NEW.digest_sha256) "
        "OR EXISTS (SELECT 1 FROM watchlist_quant_v6_artifacts "
        "WHERE digest_sha256 = NEW.digest_sha256 "
        "AND kind = NEW.kind)"
    ),
    "watchlist_quant_v6_publications": (
        "(NEW.id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM watchlist_quant_v6_publications "
        "WHERE id = NEW.id)) "
        "OR EXISTS (SELECT 1 FROM watchlist_quant_v6_publications "
        "WHERE registration_id = NEW.registration_id) "
        "OR EXISTS (SELECT 1 FROM watchlist_quant_v6_publications "
        "WHERE identity_sha256 = NEW.identity_sha256)"
    ),
    "watchlist_quant_v6_publication_artifacts": (
        "EXISTS (SELECT 1 "
        "FROM watchlist_quant_v6_publication_artifacts "
        "WHERE publication_id = NEW.publication_id "
        "AND member_ordinal = NEW.member_ordinal "
        "AND role = NEW.role "
        "AND artifact_ordinal = NEW.artifact_ordinal) "
        "OR EXISTS (SELECT 1 "
        "FROM watchlist_quant_v6_publication_artifacts "
        "WHERE binding_sha256 = NEW.binding_sha256)"
    ),
}


_REFERENCE_PREDICATES = {
    "watchlist_quant_v6_publications": (
        "NOT EXISTS (SELECT 1 FROM watchlist_quant_v6_registrations "
        "WHERE id = NEW.registration_id "
        "AND identity_sha256 = NEW.registration_identity_sha256 "
        "AND cohort_member_count = NEW.registered_member_count "
        "AND json_type(registration_json, '$.cohort.member_count') = 'integer' "
        "AND json_extract(registration_json, '$.cohort.member_count') "
        "= NEW.registered_member_count "
        "AND json_type(registration_json, '$.cohort.members') = 'array' "
        "AND json_array_length(registration_json, '$.cohort.members') "
        "= NEW.registered_member_count)"
    ),
    "watchlist_quant_v6_publication_artifacts": (
        "NOT EXISTS (SELECT 1 "
        "FROM watchlist_quant_v6_publications AS publication "
        "JOIN watchlist_quant_v6_registrations AS registration "
        "ON registration.id = publication.registration_id "
        "AND registration.identity_sha256 "
        "= publication.registration_identity_sha256 "
        "AND registration.cohort_member_count "
        "= publication.registered_member_count "
        "WHERE publication.id = NEW.publication_id "
        "AND NEW.member_ordinal >= 0 "
        "AND NEW.member_ordinal < publication.registered_member_count "
        "AND json_type(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || ']') = 'object' "
        "AND json_type(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].ordinal') = 'integer' "
        "AND json_extract(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].ordinal') "
        "= NEW.member_ordinal "
        "AND json_type(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].symbol') = 'text' "
        "AND json_extract(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].symbol') = NEW.symbol "
        "AND json_type(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].market') = 'text' "
        "AND json_extract(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].market') = NEW.market) "
        "OR NOT EXISTS (SELECT 1 FROM watchlist_quant_v6_artifacts "
        "WHERE digest_sha256 = NEW.artifact_sha256 "
        "AND kind = NEW.artifact_kind)"
    ),
}


def _trigger_definitions() -> dict[str, str]:
    definitions: dict[str, str] = {}
    for table_name in _TABLES:
        for operation in ("UPDATE", "DELETE"):
            trigger_name = f"trg_{table_name}_no_{operation.lower()}"
            definitions[trigger_name] = (
                f"CREATE TRIGGER {trigger_name} "
                f"BEFORE {operation} ON {table_name} "
                "BEGIN "
                f"SELECT RAISE(ABORT, '{table_name} is append-only'); "
                "END"
            )
        duplicate_trigger = f"trg_{table_name}_no_duplicate_key"
        definitions[duplicate_trigger] = (
            f"CREATE TRIGGER {duplicate_trigger} "
            f"BEFORE INSERT ON {table_name} "
            f"WHEN {_DUPLICATE_PREDICATES[table_name]} "
            "BEGIN "
            f"SELECT RAISE(ABORT, '{table_name} duplicate key'); "
            "END"
        )
        reference_predicate = _REFERENCE_PREDICATES.get(table_name)
        if reference_predicate is not None:
            reference_trigger = f"trg_{table_name}_validate_reference"
            definitions[reference_trigger] = (
                f"CREATE TRIGGER {reference_trigger} "
                f"BEFORE INSERT ON {table_name} "
                f"WHEN {reference_predicate} "
                "BEGIN "
                f"SELECT RAISE(ABORT, '{table_name} invalid reference'); "
                "END"
            )
    return definitions


def _install_immutable_triggers() -> None:
    for trigger_ddl in _trigger_definitions().values():
        op.execute(trigger_ddl)


def upgrade() -> None:
    op.create_table(
        "watchlist_quant_v6_registrations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("identity_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=100), nullable=False),
        sa.Column(
            "selection_rule_version",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column("algorithm_version", sa.String(length=160), nullable=False),
        sa.Column(
            "semantic_digest_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "evaluator_digest_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "acquisition_spec_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("cohort_source", sa.String(length=48), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column(
            "source_snapshot_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "cohort_manifest_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("cohort_member_count", sa.Integer(), nullable=False),
        sa.Column("schedule_sha256", sa.String(length=64), nullable=False),
        sa.Column("training_session_count", sa.Integer(), nullable=False),
        sa.Column("target_session_count", sa.Integer(), nullable=False),
        sa.Column("first_training_session_date", sa.Date(), nullable=False),
        sa.Column("first_target_session_date", sa.Date(), nullable=False),
        sa.Column("last_target_session_date", sa.Date(), nullable=False),
        sa.Column(
            "data_cutoff_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("bar_period", sa.String(length=16), nullable=False),
        sa.Column("adjustment_mode", sa.String(length=16), nullable=False),
        sa.Column("registration_json", sa.Text(), nullable=False),
        sa.Column("server_generated", sa.Boolean(), nullable=False),
        sa.Column("short_entry_allowed", sa.Boolean(), nullable=False),
        sa.Column("position_add_on_allowed", sa.Boolean(), nullable=False),
        sa.Column("order_submission_allowed", sa.Boolean(), nullable=False),
        sa.Column(
            "automatic_promotion_allowed",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column(
            "cohort_observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "identity_sha256",
            name="uq_watchlist_quant_v6_registration_identity",
        ),
        sa.UniqueConstraint(
            "id",
            "identity_sha256",
            name="uq_watchlist_quant_v6_registration_id_identity",
        ),
        sa.CheckConstraint(
            "length(identity_sha256) = 64 "
            "AND identity_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_identity_sha",
        ),
        sa.CheckConstraint(
            "schema_version = 1",
            name="ck_watchlist_quant_v6_registration_schema",
        ),
        sa.CheckConstraint(
            "length(contract_version) > 0 "
            "AND contract_version = trim(contract_version) "
            "AND length(selection_rule_version) > 0 "
            "AND selection_rule_version = trim(selection_rule_version) "
            "AND length(algorithm_version) > 0 "
            "AND algorithm_version = trim(algorithm_version)",
            name="ck_watchlist_quant_v6_registration_versions",
        ),
        sa.CheckConstraint(
            "length(semantic_digest_sha256) = 64 "
            "AND semantic_digest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_semantic_sha",
        ),
        sa.CheckConstraint(
            "length(evaluator_digest_sha256) = 64 "
            "AND evaluator_digest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_evaluator_sha",
        ),
        sa.CheckConstraint(
            "length(acquisition_spec_sha256) = 64 "
            "AND acquisition_spec_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_acquisition_sha",
        ),
        sa.CheckConstraint(
            "cohort_source = 'ROTATION_RESEARCH_CATALOG_PIT'",
            name="ck_watchlist_quant_v6_registration_cohort_source",
        ),
        sa.CheckConstraint(
            "market IN ('US', 'HK')",
            name="ck_watchlist_quant_v6_registration_market",
        ),
        sa.CheckConstraint(
            "length(source_snapshot_sha256) = 64 "
            "AND source_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_source_sha",
        ),
        sa.CheckConstraint(
            "length(cohort_manifest_sha256) = 64 "
            "AND cohort_manifest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_cohort_sha",
        ),
        sa.CheckConstraint(
            "cohort_member_count > 0",
            name="ck_watchlist_quant_v6_registration_member_count",
        ),
        sa.CheckConstraint(
            "length(schedule_sha256) = 64 "
            "AND schedule_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_schedule_sha",
        ),
        sa.CheckConstraint(
            "training_session_count = 10 AND target_session_count = 30",
            name="ck_watchlist_quant_v6_registration_session_counts",
        ),
        sa.CheckConstraint(
            "first_training_session_date < first_target_session_date "
            "AND first_target_session_date <= last_target_session_date",
            name="ck_watchlist_quant_v6_registration_session_dates",
        ),
        sa.CheckConstraint(
            "bar_period = 'MIN_5' AND adjustment_mode = 'NO_ADJUST'",
            name="ck_watchlist_quant_v6_registration_bar_source",
        ),
        sa.CheckConstraint(
            "json_valid(registration_json) = 1 "
            "AND json_type(registration_json) = 'object'",
            name="ck_watchlist_quant_v6_registration_json",
        ),
        sa.CheckConstraint(
            "server_generated = 1",
            name="ck_watchlist_quant_v6_registration_server_generated",
        ),
        sa.CheckConstraint(
            "short_entry_allowed = 0 "
            "AND position_add_on_allowed = 0 "
            "AND order_submission_allowed = 0 "
            "AND automatic_promotion_allowed = 0",
            name="ck_watchlist_quant_v6_registration_p0",
        ),
        sa.CheckConstraint(
            "data_cutoff_at <= cohort_observed_at "
            "AND cohort_observed_at <= registered_at",
            name="ck_watchlist_quant_v6_registration_times",
        ),
    )
    op.create_index(
        "ix_watchlist_quant_v6_registration_market_target_registered",
        "watchlist_quant_v6_registrations",
        ["market", "last_target_session_date", "registered_at"],
    )
    op.create_index(
        "ix_watchlist_quant_v6_registration_registered_id",
        "watchlist_quant_v6_registrations",
        ["registered_at", "id"],
    )

    op.create_table(
        "watchlist_quant_v6_artifacts",
        sa.Column("digest_sha256", sa.String(length=64), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("codec", sa.String(length=16), nullable=False),
        sa.Column("compression_level", sa.Integer(), nullable=False),
        sa.Column("raw_size", sa.Integer(), nullable=False),
        sa.Column("compressed_size", sa.Integer(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "digest_sha256",
            "kind",
            name="uq_watchlist_quant_v6_artifact_digest_kind",
        ),
        sa.CheckConstraint(
            "length(digest_sha256) = 64 "
            "AND digest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_artifact_digest_sha",
        ),
        sa.CheckConstraint(
            "schema_version = 1",
            name="ck_watchlist_quant_v6_artifact_schema",
        ),
        sa.CheckConstraint(
            "kind IN ('WATCHLIST_QUANT_V6_ASSESSMENT', "
            "'WATCHLIST_QUANT_V6_SESSION_INPUT', 'WATCHLIST_QUANT_V6_EVENT')",
            name="ck_watchlist_quant_v6_artifact_kind",
        ),
        sa.CheckConstraint(
            "codec = 'zlib' AND compression_level = 9",
            name="ck_watchlist_quant_v6_artifact_codec",
        ),
        sa.CheckConstraint(
            "raw_size >= 1 AND raw_size <= 2097152",
            name="ck_watchlist_quant_v6_artifact_raw_size",
        ),
        sa.CheckConstraint(
            "compressed_size >= 1 AND compressed_size <= 524288",
            name="ck_watchlist_quant_v6_artifact_compressed_size",
        ),
        sa.CheckConstraint(
            "length(payload) = compressed_size",
            name="ck_watchlist_quant_v6_artifact_payload_size",
        ),
    )
    op.create_index(
        "ix_watchlist_quant_v6_artifact_kind_created",
        "watchlist_quant_v6_artifacts",
        ["kind", "created_at"],
    )

    op.create_table(
        "watchlist_quant_v6_publications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("registration_id", sa.Integer(), nullable=False),
        sa.Column(
            "registration_identity_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("identity_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("publication_json", sa.Text(), nullable=False),
        sa.Column("registered_member_count", sa.Integer(), nullable=False),
        sa.Column("assessment_artifact_count", sa.Integer(), nullable=False),
        sa.Column("session_input_artifact_count", sa.Integer(), nullable=False),
        sa.Column("event_artifact_count", sa.Integer(), nullable=False),
        sa.Column("binding_count", sa.Integer(), nullable=False),
        sa.Column("promotion_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "automatic_promotion_allowed",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("order_submission_allowed", sa.Boolean(), nullable=False),
        sa.Column("short_entry_allowed", sa.Boolean(), nullable=False),
        sa.Column("position_add_on_allowed", sa.Boolean(), nullable=False),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "registration_id",
            name="uq_watchlist_quant_v6_publication_registration",
        ),
        sa.UniqueConstraint(
            "identity_sha256",
            name="uq_watchlist_quant_v6_publication_identity",
        ),
        sa.ForeignKeyConstraint(
            ["registration_id", "registration_identity_sha256"],
            [
                "watchlist_quant_v6_registrations.id",
                "watchlist_quant_v6_registrations.identity_sha256",
            ],
            name="fk_watchlist_quant_v6_publication_registration_identity",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(registration_identity_sha256) = 64 "
            "AND registration_identity_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_publication_registration_sha",
        ),
        sa.CheckConstraint(
            "length(identity_sha256) = 64 "
            "AND identity_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_publication_identity_sha",
        ),
        sa.CheckConstraint(
            "schema_version = 1",
            name="ck_watchlist_quant_v6_publication_schema",
        ),
        sa.CheckConstraint(
            "length(contract_version) > 0 "
            "AND contract_version = trim(contract_version)",
            name="ck_watchlist_quant_v6_publication_contract",
        ),
        sa.CheckConstraint(
            "status = 'PUBLISHED'",
            name="ck_watchlist_quant_v6_publication_status",
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64 "
            "AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_publication_manifest_sha",
        ),
        sa.CheckConstraint(
            "json_valid(publication_json) = 1 "
            "AND json_type(publication_json) = 'object'",
            name="ck_watchlist_quant_v6_publication_json",
        ),
        sa.CheckConstraint(
            "registered_member_count > 0 "
            "AND assessment_artifact_count = registered_member_count "
            "AND session_input_artifact_count >= 0 "
            "AND event_artifact_count >= 0 "
            "AND binding_count = assessment_artifact_count "
            "+ session_input_artifact_count + event_artifact_count",
            name="ck_watchlist_quant_v6_publication_counts",
        ),
        sa.CheckConstraint(
            "promotion_eligible = 0 "
            "AND automatic_promotion_allowed = 0 "
            "AND order_submission_allowed = 0 "
            "AND short_entry_allowed = 0 "
            "AND position_add_on_allowed = 0",
            name="ck_watchlist_quant_v6_publication_p0",
        ),
    )
    op.create_index(
        "ix_watchlist_quant_v6_publication_published_id",
        "watchlist_quant_v6_publications",
        ["published_at", "id"],
    )

    op.create_table(
        "watchlist_quant_v6_publication_artifacts",
        sa.Column("publication_id", sa.Integer(), primary_key=True),
        sa.Column("member_ordinal", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("role", sa.String(length=20), primary_key=True),
        sa.Column("artifact_ordinal", sa.Integer(), primary_key=True),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_kind", sa.String(length=48), nullable=False),
        sa.Column("binding_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["watchlist_quant_v6_publications.id"],
            name="fk_watchlist_quant_v6_binding_publication",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_sha256", "artifact_kind"],
            [
                "watchlist_quant_v6_artifacts.digest_sha256",
                "watchlist_quant_v6_artifacts.kind",
            ],
            name="fk_watchlist_quant_v6_binding_artifact_identity",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "binding_sha256",
            name="uq_watchlist_quant_v6_binding_sha",
        ),
        sa.CheckConstraint(
            "member_ordinal >= 0 AND artifact_ordinal >= 0",
            name="ck_watchlist_quant_v6_binding_ordinals",
        ),
        sa.CheckConstraint(
            "length(symbol) > 3 AND length(symbol) <= 50 "
            "AND symbol = trim(symbol) AND symbol = upper(symbol)",
            name="ck_watchlist_quant_v6_binding_symbol",
        ),
        sa.CheckConstraint(
            "market IN ('US', 'HK') "
            "AND ((market = 'US' AND symbol LIKE '%.US') "
            "OR (market = 'HK' AND symbol LIKE '%.HK'))",
            name="ck_watchlist_quant_v6_binding_market",
        ),
        sa.CheckConstraint(
            "role IN ('ASSESSMENT', 'SESSION_INPUT', 'EVENT')",
            name="ck_watchlist_quant_v6_binding_role",
        ),
        sa.CheckConstraint(
            "artifact_kind IN ('WATCHLIST_QUANT_V6_ASSESSMENT', "
            "'WATCHLIST_QUANT_V6_SESSION_INPUT', 'WATCHLIST_QUANT_V6_EVENT')",
            name="ck_watchlist_quant_v6_binding_kind",
        ),
        sa.CheckConstraint(
            "(role = 'ASSESSMENT' "
            "AND artifact_kind = 'WATCHLIST_QUANT_V6_ASSESSMENT' "
            "AND session_date IS NULL AND artifact_ordinal = 0) "
            "OR (role = 'SESSION_INPUT' "
            "AND artifact_kind = 'WATCHLIST_QUANT_V6_SESSION_INPUT' "
            "AND session_date IS NOT NULL AND artifact_ordinal < 30) "
            "OR (role = 'EVENT' "
            "AND artifact_kind = 'WATCHLIST_QUANT_V6_EVENT' "
            "AND session_date IS NOT NULL)",
            name="ck_watchlist_quant_v6_binding_role_kind_session",
        ),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64 "
            "AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_binding_artifact_sha",
        ),
        sa.CheckConstraint(
            "length(binding_sha256) = 64 "
            "AND binding_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_binding_sha256",
        ),
    )
    op.create_index(
        "ix_watchlist_quant_v6_binding_member_session_role",
        "watchlist_quant_v6_publication_artifacts",
        ["publication_id", "member_ordinal", "session_date", "role"],
    )
    op.create_index(
        "ix_watchlist_quant_v6_binding_artifact_sha",
        "watchlist_quant_v6_publication_artifacts",
        ["artifact_sha256"],
    )
    _install_immutable_triggers()


def downgrade() -> None:
    for trigger_name in reversed(tuple(_trigger_definitions())):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

    op.drop_index(
        "ix_watchlist_quant_v6_binding_artifact_sha",
        table_name="watchlist_quant_v6_publication_artifacts",
    )
    op.drop_index(
        "ix_watchlist_quant_v6_binding_member_session_role",
        table_name="watchlist_quant_v6_publication_artifacts",
    )
    op.drop_table("watchlist_quant_v6_publication_artifacts")

    op.drop_index(
        "ix_watchlist_quant_v6_publication_published_id",
        table_name="watchlist_quant_v6_publications",
    )
    op.drop_table("watchlist_quant_v6_publications")

    op.drop_index(
        "ix_watchlist_quant_v6_artifact_kind_created",
        table_name="watchlist_quant_v6_artifacts",
    )
    op.drop_table("watchlist_quant_v6_artifacts")

    op.drop_index(
        "ix_watchlist_quant_v6_registration_registered_id",
        table_name="watchlist_quant_v6_registrations",
    )
    op.drop_index(
        "ix_watchlist_quant_v6_registration_market_target_registered",
        table_name="watchlist_quant_v6_registrations",
    )
    op.drop_table("watchlist_quant_v6_registrations")
