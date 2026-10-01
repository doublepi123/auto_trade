"""Stdin-only offline non-authorizing diagnostics."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from app.domain.passive_allocation.eligibility import evaluate
from app.domain.passive_allocation.eligibility_codec import parse_input, parse_time
from app.domain.passive_allocation.eligibility_model import EligibilityReport, InputError, Reason, Status


def main() -> int:
    args = sys.argv[1:]
    report = EligibilityReport(Status.INVALID, (Reason.INVALID_ARGUMENTS,))
    # No argparse error may echo user-supplied flags or values.
    if len(args) == 2 and args[0] == "--as-of":
        try:
            now = parse_time(args[1])
        except ValueError:
            report = EligibilityReport(Status.INVALID, (Reason.INVALID_AS_OF,))
        else:
            try:
                evidence = parse_input(sys.stdin.buffer.read(65537))
                report = EligibilityReport(Status.INVALID, (evidence.code,)) if isinstance(evidence, InputError) else evaluate(evidence, now=now)
            except (OSError, ValueError):
                report = EligibilityReport(Status.INVALID, (Reason.INVALID_INPUT,))
    print(json.dumps(asdict(report), sort_keys=True, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
