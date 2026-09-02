# `backend/app/domain/` — Pure Computation Layer

## OVERVIEW
56 files, ~24k lines, 9 subpackages. The cleanest layer in the repo: **zero imports from `app.services`**, verified by AST scan. Depends only on `app.core` calendar utilities (26 imports of `market_calendar` / `holiday_calendar`).

## STRUCTURE
```
domain/
├── strategy_v2/            14 files, 7129 lines — shadow engine, bracket, profit_lock,
│                           portfolio_routing, signal_edge, clustered_returns,
│                           trusted_frozen_assessment (1749), PREREGISTRATION.md
├── universe_selection/      7 files, 4986 — catalog, selector, rotation walk-forward,
│                           data/ index-membership snapshots + THIRD_PARTY_NOTICES.md
├── llm_interval_forward/    4 files, 4030 — replay.py (2164) + contract
├── watchlist_quant_v6/      5 files, 3350 — quote-only historical evaluation
├── prompt/                 10 files,  566 — see prompt/AGENTS.md
├── analysis/ sentiment/ performance/ experiment/   small helper packages
```

## PURITY CONTRACT
- **Allowed imports**: stdlib, other `domain` modules, and `app.core` pure utilities (calendars). Nothing else.
- **Forbidden**: `app.services.*`, `app.api.*`, `app.platform.*`, `SessionLocal` / any ORM session, `BrokerGateway`, `settings.*`, network calls, `datetime.now()` without an injected clock.
- Data in, values out. Anything needing I/O belongs in `services/` — pass the result in through parameters.
- Root `AGENTS.md` says "no I/O"; the precise statement is *no services/DB/network, core calendars permitted*.

## WHERE TO LOOK
| Task | Location |
|---|---|
| Change shadow entry/exit logic | `strategy_v2/` (engine, bracket, profit_lock) |
| Prove/refute a signal has edge | `strategy_v2/signal_edge.py` + `clustered_returns.py` |
| Touch frozen v5 parameters | **read `strategy_v2/PREREGISTRATION.md` first** |
| Candidate pool ranking / rotation | `universe_selection/` |
| Quote-only historical evidence | `watchlist_quant_v6/` |
| LLM prompt assembly | `prompt/` (has its own AGENTS.md) |

## ANTI-PATTERNS (THIS DIR)
- Importing anything from `app.services` — this layer has zero such imports today and that invariant is what makes it testable without fixtures.
- Reading `settings` directly — accept configuration as arguments.
- Reading the wall clock — take `now` / a clock callable as a parameter so replay stays deterministic.
- Editing frozen v5 parameters to "improve" them: `test_strategy_v2_preregistration.py` pins a SHA-256 over the parameter set. The fix is a preregistration decision plus a new `algorithm_version`, in the same commit as the hash and the markdown — never a hash bump alone.
- Reporting thin evidence as `FAIL`. `INSUFFICIENT_DATA` is a distinct verdict and must stay distinct.
- Counting first-passage outcomes across barrier versions — a different barrier means a different driftless baseline, so cohorts must not be merged.

## COMMANDS
```bash
cd backend
python3 -m pytest tests/test_signal_edge.py tests/test_strategy_v2_preregistration.py -v
python3 -m basedpyright app/domain/
```
Domain tests need no DB and no fixtures — if a new test needs a session, the code is in the wrong layer.
