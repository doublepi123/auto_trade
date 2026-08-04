#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.services.historical_ledger_import_service import (
    HistoricalLedgerImportError,
    HistoricalLedgerImportService,
    apply_result_payload,
    import_plan_payload,
)
from app.services.historical_order_completeness_reader import (
    HistoricalPreviewError,
    build_longport_historical_reader_from_env,
)


def _aware_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            "must include an explicit timezone offset"
        )
    if parsed.microsecond:
        raise argparse.ArgumentTypeError(
            "must use whole-second precision"
        )
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview a complete LongPort historical order/execution snapshot "
            "and, only with digest plus account-fingerprint authorization, "
            "atomically import its FILLED ledger evidence. This command has "
            "no order-submission or live-reconciliation path."
        )
    )
    parser.add_argument(
        "--symbol",
        required=True,
        help="explicit broker symbol, for example NVDA.US",
    )
    parser.add_argument(
        "--start-at",
        required=True,
        type=_aware_datetime,
        help="inclusive ISO-8601 start timestamp with timezone",
    )
    parser.add_argument(
        "--end-at",
        required=True,
        type=_aware_datetime,
        help="inclusive ISO-8601 end timestamp with timezone",
    )
    parser.add_argument(
        "--apply-preview-digest",
        help=(
            "apply mode only: exact lowercase digest printed by a prior "
            "preview; omitting this keeps the command read-only"
        ),
    )
    parser.add_argument(
        "--account-fingerprint",
        help=(
            "apply mode only: exact broker_identity_fingerprint printed by "
            "the same preview"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if bool(args.apply_preview_digest) != bool(args.account_fingerprint):
        parser.error(
            "--apply-preview-digest and --account-fingerprint must be "
            "provided together"
        )
    try:
        reader = build_longport_historical_reader_from_env()
        service = HistoricalLedgerImportService(SessionLocal, reader)
        if args.apply_preview_digest:
            result = service.apply(
                symbol=args.symbol,
                start_at=args.start_at,
                end_at=args.end_at,
                expected_preview_digest=args.apply_preview_digest,
                expected_broker_identity_fingerprint=(
                    args.account_fingerprint
                ),
            )
            payload = apply_result_payload(result)
            exit_code = 0
        else:
            plan = service.preview(
                symbol=args.symbol,
                start_at=args.start_at,
                end_at=args.end_at,
            )
            payload = import_plan_payload(plan)
            exit_code = 0 if plan.can_apply else 1
    except (HistoricalPreviewError, HistoricalLedgerImportError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "mode": (
                        "APPLY_FAILED"
                        if args.apply_preview_digest
                        else "PREVIEW_FAILED"
                    ),
                    "error": str(exc),
                    "database_mutated": False,
                    "order_submission_allowed": False,
                    "live_reconciliation_triggered": False,
                    "cost_basis_inferred": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
