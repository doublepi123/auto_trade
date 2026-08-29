#!/usr/bin/env python3
"""Offline screen of platform strategy plugins on minute bars.

Read-only research: never submits orders.

Volume=0: JSON rows are five fields (timestamp, open, high, low, close) with
no volume. An audit found the only volume consumer is
VolumeShareSlippageModel (app/platform/fill_model.py:49-52), which is opt-in
and returns zero slippage when volume<=0. Bars are loaded with volume=0;
that is the dataset, not a silent fill.

VolumeShareSlippageModel is prohibited in every cost scenario. On a
volume-less dataset it degrades to zero slippage and manufactures a fake
low-cost result. Scenarios keep fill_model=None and vary PaperBrokerConfig
commission/tick fields instead.

First-passage is not_applicable. These plugins have no stop/target brackets;
they exit on their own signal. Feeding 0/0 into assess_first_passage /
assess_signal_edge would force INSUFFICIENT_DATA regardless of evidence.
assess_plugin_edge judges on the clustered t-test alone and applies the
evidence floors itself. This is a WEAKER standard than the live signal gate,
which had to clear BOTH first-passage against a random-walk baseline AND
cluster-robust significance. A PASS here is not equivalent to the
incumbent's bar and must not be read as one.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.platform.strategy_plugin_inventory import (
    DEFAULT_PLUGIN_PARAMS,
    CostScenario,
    InventoryRow,
    PluginEdgeAssessment,
    PluginRunRequest,
    PluginRunResult,
    ScreenRequest,
    ScreenResult,
    assess_plugin_edge,
    cost_scenarios,
    discover_plugin_names,
    load_minute_bars,
    run_plugin_offline,
    screen_inventory,
)

__all__ = [
    "DEFAULT_PLUGIN_PARAMS",
    "CostScenario",
    "InventoryRow",
    "PluginEdgeAssessment",
    "PluginRunRequest",
    "PluginRunResult",
    "ScreenRequest",
    "ScreenResult",
    "assess_plugin_edge",
    "cost_scenarios",
    "discover_plugin_names",
    "load_minute_bars",
    "main",
    "run_plugin_offline",
    "screen_inventory",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline inventory screen of platform strategy plugins."
    )
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--json-out", type=Path)
    return parser


def _print_report(result: ScreenResult, *, symbol: str) -> None:
    print("plugin\tcost\ttrips\tdays\tgross\tnet\tverdict")
    for row in result.rows:
        print(
            f"{row.plugin_name}\t{row.cost_scenario}\t{row.round_trip_count}\t"
            f"{row.distinct_days}\t{row.gross_pnl}\t{row.net_pnl}\t{row.verdict}"
        )
    print()
    print("CAVEATS")
    print(f"- Single symbol ({symbol}); results do not generalise across a universe.")
    print("- Sample covers 38 trading days of minute bars.")
    print(
        "- First-passage is not_applicable: plugins exit on their own signal, "
        "not stop/target brackets."
    )
    print(
        "- This clustered-t-only bar is WEAKER than the live signal gate "
        "(first-passage vs random-walk AND cluster-robust significance). "
        "PASS here is not equivalent to the incumbent's bar."
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    data_path: Path = args.data
    symbol: str = args.symbol
    json_out: Path | None = args.json_out
    result = screen_inventory(
        ScreenRequest(
            bars=load_minute_bars(data_path, symbol=symbol),
            symbol=symbol,
        )
    )
    _print_report(result, symbol=symbol)
    if json_out is not None:
        payload = [
            {
                "plugin_name": row.plugin_name,
                "cost_scenario": row.cost_scenario,
                "round_trip_count": row.round_trip_count,
                "distinct_days": row.distinct_days,
                "gross_pnl": str(row.gross_pnl),
                "net_pnl": str(row.net_pnl),
                "fees": str(row.fees),
                "verdict": row.verdict,
                "first_passage_applicability": row.first_passage_applicability,
            }
            for row in result.rows
        ]
        json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
