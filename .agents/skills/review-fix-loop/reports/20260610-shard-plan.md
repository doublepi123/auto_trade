# Review-Fix Loop — Shard Plan (2026-06-10)

## Target: Entire repository (~44,930 lines)
## Baseline SHA256: d19e38168e44e13fa695bda03685f40d3cde401c254f480e257c1db742bdefde

## Shards

### Shard A: Trading Core (engine, risk, runner)
- `backend/app/core/engine.py`
- `backend/app/core/risk.py`
- `backend/app/core/market_calendar.py`
- `backend/app/core/fees.py`
- `backend/app/runner.py`

### Shard B: Broker & Data Infrastructure
- `backend/app/core/broker.py`
- `backend/app/core/backtest.py`
- `backend/app/core/audit.py`
- `backend/app/core/credential_crypto.py`
- `backend/app/core/notify.py`
- `backend/app/core/notifiers/` (all files)
- `backend/app/services/data_aggregator.py`

### Shard C: Services Layer
- `backend/app/services/trade_execution_service.py`
- `backend/app/services/strategy_service.py`
- `backend/app/services/runtime_state_service.py`
- `backend/app/services/daily_pnl_service.py`
- `backend/app/services/credentials_service.py`
- `backend/app/services/event_list_service.py`
- `backend/app/services/trade_event_service.py`
- `backend/app/services/watchlist_service.py`
- `backend/app/services/report_service.py`
- `backend/app/services/review_service.py`
- `backend/app/services/experiment_grid_service.py`
- `backend/app/services/strategy_experiment_service.py`

### Shard D: API Layer & Config
- `backend/app/api/` (all files)
- `backend/app/schemas.py`
- `backend/app/models.py`
- `backend/app/database.py`
- `backend/app/main.py`
- `backend/app/config.py`

### Shard E: LLM & Domain Logic
- `backend/app/domain/` (all files)
- `backend/app/services/llm_advisor_service.py`
- `backend/app/services/interval_application_service.py`
- `backend/app/services/llm_interaction_service.py`
- `backend/app/services/llm_recommendation_evaluator.py`
- `backend/app/services/llm_symbol_state_service.py`

### Shard F: Frontend
- `frontend/src/` (all .ts, .vue files)

### Shard G: Tests & Infrastructure
- `backend/tests/` (all test files)
- `docker-compose.yaml`, `docker-compose.dockerhub.yaml`
- `.github/workflows/dockerhub.yml`
- `pyrightconfig.json`
