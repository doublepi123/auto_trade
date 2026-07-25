#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.core.broker import BrokerGateway
from app.domain.universe_selection import (
    INDEX_CANDIDATE_CATALOG,
    ROTATION_BENCHMARK_SYMBOLS,
    completed_daily_bars,
    evaluate_rotation_walk_forward,
)
from app.services.universe_selection_service import (
    selection_config_from_settings,
)


def _configure_longport_environment() -> None:
    credentials = {
        "LONGPORT_APP_KEY": settings.longbridge_app_key,
        "LONGPORT_APP_SECRET": settings.longbridge_app_secret,
        "LONGPORT_ACCESS_TOKEN": settings.longbridge_access_token,
    }
    missing = [
        name for name, value in credentials.items() if not value
    ]
    if missing:
        raise RuntimeError(
            "Longport credentials are unavailable: "
            + ", ".join(missing)
        )
    for name, value in credentials.items():
        os.environ[name] = value


def _summary(payload: dict[str, object]) -> dict[str, object]:
    variants = payload.get("variants")
    compact_variants: list[dict[str, object]] = []
    if isinstance(variants, (list, tuple)):
        for raw in variants:
            if not isinstance(raw, dict):
                continue
            compact_variants.append(
                {
                    "name": (
                        raw.get("variant", {}).get("name")
                        if isinstance(raw.get("variant"), dict)
                        else None
                    ),
                    "training_score": raw.get("training_score"),
                    "validation_passed": raw.get(
                        "validation_passed"
                    ),
                    "validation_blockers": raw.get(
                        "validation_blockers"
                    ),
                    "full": raw.get("full"),
                    "training": raw.get("training"),
                    "validation": raw.get("validation"),
                }
            )
    return {
        "algorithm_version": payload.get("algorithm_version"),
        "status": payload.get("status"),
        "data_scope": payload.get("data_scope"),
        "survivorship_bias": payload.get("survivorship_bias"),
        "selected_variant": payload.get("selected_variant"),
        "selected_variant_validation_passed": payload.get(
            "selected_variant_validation_passed"
        ),
        "validated_challenger_variant": payload.get(
            "validated_challenger_variant"
        ),
        "automatic_promotion_allowed": payload.get(
            "automatic_promotion_allowed"
        ),
        "promotion_blockers": payload.get("promotion_blockers"),
        "variants": compact_variants,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the read-only monthly index momentum rotation "
            "with causal month-open fills and QQQ/DIA benchmarks."
        )
    )
    parser.add_argument(
        "--history-bars",
        type=int,
        default=1000,
        help="recent completed daily bars requested per symbol",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="include period-level evidence instead of the compact summary",
    )
    args = parser.parse_args()
    if args.history_bars < 300:
        parser.error("--history-bars must be at least 300")
    if args.history_bars > 1000:
        parser.error("--history-bars must not exceed 1000")

    _configure_longport_environment()
    now = datetime.now(timezone.utc)
    broker = BrokerGateway()
    try:
        bars_by_symbol = {}
        symbols = [
            candidate.symbol
            for candidate in INDEX_CANDIDATE_CATALOG
        ]
        for index, symbol in enumerate(symbols, start=1):
            print(
                f"candidate {index}/{len(symbols)} {symbol}",
                file=sys.stderr,
            )
            raw = broker.get_forward_adjusted_candlesticks(
                symbol,
                "DAY",
                args.history_bars,
            )
            bars_by_symbol[symbol] = completed_daily_bars(
                raw,
                market="US",
                now=now,
            )
        benchmark_bars = {}
        for symbol in ROTATION_BENCHMARK_SYMBOLS:
            print(f"benchmark {symbol}", file=sys.stderr)
            raw = broker.get_forward_adjusted_candlesticks(
                symbol,
                "DAY",
                args.history_bars,
            )
            benchmark_bars[symbol] = completed_daily_bars(
                raw,
                market="US",
                now=now,
            )
        payload = evaluate_rotation_walk_forward(
            candidates=INDEX_CANDIDATE_CATALOG,
            bars_by_symbol=bars_by_symbol,
            benchmark_bars_by_symbol=benchmark_bars,
            base_config=selection_config_from_settings(),
        ).to_dict()
        print(
            json.dumps(
                payload if args.full else _summary(payload),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
