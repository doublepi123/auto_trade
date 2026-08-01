#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.strategy_v2.frozen_disproof_queue import (
    ASSESSMENT_POLICY_VERSION,
    evaluate_frozen_forward_disproof_queue,
)


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be an ISO date (YYYY-MM-DD)"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate a precommitted, research-only forward disproof queue "
            "from explicitly supplied Strategy v2 forward-validation JSON."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="JSON input; this command never reads the broker or database",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional JSON output path (stdout when omitted)",
    )
    parser.add_argument(
        "--assessment-as-of-date",
        type=_iso_date,
        help=(
            "explicit NYSE calendar cutoff for the assessment denominator; "
            "required here when the input JSON has no assessment_policy"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        with args.input.open("r", encoding="utf-8") as source:
            payload = json.load(source)
        if args.assessment_as_of_date is not None:
            if not isinstance(payload, dict):
                raise ValueError("input JSON must be an object")
            supplied_policy = payload.get("assessment_policy")
            cli_date = args.assessment_as_of_date.isoformat()
            if supplied_policy is None:
                payload["assessment_policy"] = {
                    "version": ASSESSMENT_POLICY_VERSION,
                    "assessment_as_of_date": cli_date,
                }
            elif (
                not isinstance(supplied_policy, dict)
                or supplied_policy.get("version")
                != ASSESSMENT_POLICY_VERSION
                or supplied_policy.get("assessment_as_of_date") != cli_date
            ):
                raise ValueError(
                    "--assessment-as-of-date conflicts with input "
                    "assessment_policy"
                )
        report = evaluate_frozen_forward_disproof_queue(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if args.output is None:
        print(encoded)
    else:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
