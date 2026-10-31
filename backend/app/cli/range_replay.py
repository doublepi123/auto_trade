"""Offline range research CLI. No broker, application settings or DB imports."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from collections.abc import Sequence

from app.domain.range_replay import RangeReplayConfig
from app.services.range_research_service import evaluate_tapes, register_forward


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register", help="create immutable forward evidence identity")
    register.add_argument("--output", type=Path, required=True)
    register.add_argument("--phase", choices=("entry", "crossday"), default="entry")
    evaluate = sub.add_parser("evaluate", help="compare independent arms on recorded BBO")
    evaluate.add_argument("--tape-root", type=Path, required=True)
    evaluate.add_argument("--start", type=date.fromisoformat, required=True)
    evaluate.add_argument("--end", type=date.fromisoformat, required=True)
    evaluate.add_argument("--registration", type=Path)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--phase", choices=("entry", "crossday"), default="entry")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    config = RangeReplayConfig()
    try:
        if args.command == "register":
            result = register_forward(args.output, config, now=now, phase=args.phase)
        else:
            # Fail before reading tapes if the immutable output already exists.
            if args.output.exists():
                raise FileExistsError("research output already exists")
            result = evaluate_tapes(
                args.tape_root, args.start, args.end, config=config,
                now=now, registration=args.registration, phase=args.phase,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+"\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"range research refused: {exc}\n")
    print(json.dumps({
        "output": str(args.output), "status": result.get("status", "REGISTERED"),
        "automatic_promotion": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
