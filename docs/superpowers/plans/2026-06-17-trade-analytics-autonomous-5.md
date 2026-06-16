# Trade Analytics Autonomous 5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development while implementing. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five low-risk, read-only analytics views over closed round-trip trades.

**Architecture:** Reuse `DailyPnlService.pair_round_trips()` as the only source of closed-trade truth. Add one pure aggregation service and expose it through `/api/trades/analytics/*` endpoints with Pydantic schemas. No new tables, migrations, broker calls, runner changes, or order-path changes.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy session dependency, pytest.

---

### File Structure

- Create `backend/app/services/trade_analytics_service.py`: pure functions for calendar, hold-duration, PnL distribution, monthly summary, and weekday attribution.
- Modify `backend/app/schemas.py`: response models for the five analytics payloads.
- Modify `backend/app/api/trades.py`: route handlers under `/api/trades/analytics/*`.
- Create `backend/tests/test_trade_analytics_service.py`: pure aggregation tests.
- Modify `backend/tests/test_trades_api.py`: endpoint shape tests using the existing per-file SQLite setup.

### Tasks

- [ ] Write failing service tests for all five analytics.
- [ ] Run the service tests and confirm they fail because the service does not exist.
- [ ] Implement the pure analytics service.
- [ ] Run the service tests and confirm they pass.
- [ ] Write failing API tests for representative analytics endpoints.
- [ ] Add schemas and FastAPI routes.
- [ ] Run focused API tests.
- [ ] Run `python3 -m pytest tests/test_trade_analytics_service.py tests/test_trades_api.py -q`.
- [ ] Run `python3 -m basedpyright app/services/trade_analytics_service.py app/api/trades.py app/schemas.py`.
