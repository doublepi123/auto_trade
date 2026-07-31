#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.strategy_v2.frozen_disproof_queue import (
    evaluate_frozen_forward_disproof_queue,
)


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        with args.input.open("r", encoding="utf-8") as source:
            payload = json.load(source)
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
