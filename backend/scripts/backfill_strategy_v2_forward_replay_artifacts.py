from __future__ import annotations

import argparse
import json

from app.database import SessionLocal, init_db
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Archive immutable Strategy v2 forward replay inputs already present "
            "in SQLite; broker history is never consulted."
        )
    )
    parser.add_argument("--limit", type=int, default=250)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")

    init_db()
    with SessionLocal() as db:
        result = StrategyV2ShadowService(
            db
        ).backfill_forward_replay_artifacts(limit=args.limit)
    print(json.dumps({
        "archived": result.archived,
        "blocked_evidence_ids": list(result.blocked_evidence_ids),
        "broker_history_used": False,
        "order_submission_allowed": False,
        "automatic_promotion_allowed": False,
    }, sort_keys=True))
    return 1 if result.blocked_evidence_ids else 0


if __name__ == "__main__":
    raise SystemExit(main())
