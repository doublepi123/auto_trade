"""Per-symbol realized PnL attribution across the portfolio.

Groups ``ClosedRoundTrip`` rows by symbol into winners/losers with win-rate,
contribution share and best/worst trade — the portfolio-level symbol axis that
the single-symbol, side-keyed ReportService attribution does not provide.
Read-only; uses net PnL.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.daily_pnl_service import ClosedRoundTrip


@dataclass(frozen=True)
class SymbolAttributionRow:
    symbol: str
    realized_pnl: float
    trade_count: int
    win_count: int
    win_rate: float
    contribution_share: float
    largest_win: float | None
    largest_loss: float | None


@dataclass(frozen=True)
class SymbolAttributionResult:
    rows: list[SymbolAttributionRow]
    total_realized_pnl: float


def compute_symbol_attribution(trips: list[ClosedRoundTrip]) -> SymbolAttributionResult:
    # Accumulate per-symbol aggregates in insertion-stable buckets.
    symbols: list[str] = []
    pnl: dict[str, float] = {}
    count: dict[str, int] = {}
    wins: dict[str, int] = {}
    # Track best win / worst loss separately (None until that sign is seen) so a
    # single-sign symbol reports None for the absent side, not a mislabeled value.
    largest_win: dict[str, float | None] = {}
    largest_loss: dict[str, float | None] = {}

    for t in trips:
        s = t.symbol
        if s not in pnl:
            symbols.append(s)
            pnl[s] = 0.0
            count[s] = 0
            wins[s] = 0
            largest_win[s] = None
            largest_loss[s] = None
        pnl[s] += t.net_pnl
        count[s] += 1
        if t.net_pnl > 0:
            wins[s] += 1
            cur_win = largest_win[s]
            largest_win[s] = t.net_pnl if cur_win is None else max(cur_win, t.net_pnl)
        elif t.net_pnl < 0:
            cur_loss = largest_loss[s]
            largest_loss[s] = t.net_pnl if cur_loss is None else min(cur_loss, t.net_pnl)

    total = sum(pnl.values())

    rows: list[SymbolAttributionRow] = []
    for s in symbols:
        lw = largest_win[s]
        ll = largest_loss[s]
        rows.append(SymbolAttributionRow(
            symbol=s,
            realized_pnl=round(pnl[s], 2),
            trade_count=count[s],
            win_count=wins[s],
            win_rate=round((wins[s] / count[s]) * 100.0, 4) if count[s] else 0.0,
            # Signed share of the total realized PnL. Guard near-zero totals
            # (wins/losses cancelling to float residue) as well as exact zero,
            # since pnl/1e-13 would produce nonsensical millions-percent shares.
            contribution_share=round(pnl[s] / total, 4) if abs(total) > 1e-9 else 0.0,
            largest_win=round(lw, 2) if lw is not None else None,
            largest_loss=round(ll, 2) if ll is not None else None,
        ))
    # Biggest absolute contributors first (winners and losers both surface).
    rows.sort(key=lambda r: abs(r.realized_pnl), reverse=True)

    return SymbolAttributionResult(rows=rows, total_realized_pnl=round(total, 2))
