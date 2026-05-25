# CLAUDE.md

本文件用于指导 Claude Code 在本仓库内的工作方式。**优先级高于 `~/code/CLAUDE.md`（那份描述的是另一个项目，不适用于此处）。**

## 项目概述

`auto_trade` 是基于长桥（Longbridge / Longport）OpenAPI 的自动化**区间交易**系统：

- 后端：FastAPI + SQLAlchemy 2.0 + SQLite，运行交易状态机、风控、订单执行、Server酱通知。
- 前端：Vue 3 + Vite + Element Plus + TypeScript（Hash 路由）。
- 经纪商 SDK：`longport`（行情订阅 + 下单）。
- 可选：DeepSeek LLM 顾问，建议并应用 `buy_low` / `sell_high` 区间。
- 离线：CSV 回测（`BacktestEngine`，不发真实订单）。

**部署假设：** 仅内网使用。Docker Compose 将前端绑定 `0.0.0.0:${AUTO_TRADE_FRONTEND_PORT:-8080}`，API 经 Nginx 反代；`AUTO_TRADE_API_KEY` 可选（内网通常留空）。

## 目录结构

```
auto_trade/
├── backend/
│   ├── app/
│   │   ├── main.py                         # FastAPI 入口、lifespan、LLM 定时 cron
│   │   ├── config.py                       # pydantic-settings（AUTO_TRADE_* / LONGPORT_*）
│   │   ├── database.py                     # engine、init_db、SQLite _ensure_*（含 tracked_entries 表）
│   │   ├── models.py                       # StrategyConfig、OrderRecord、TrackedEntry、TradeEvent、LLMInteraction、RuntimeState…
│   │   ├── schemas.py                      # Pydantic API schemas
│   │   ├── runner.py                       # AppRunner：行情订阅、策略循环、WS 广播
│   │   ├── api/
│   │   │   ├── strategy.py                 # /api/strategy、/api/status、/api/status/history
│   │   │   ├── trade.py                    # 订单、账户、事件、/api/control/*
│   │   │   ├── credentials.py              # 加密凭证 CRUD
│   │   │   ├── llm_advisor.py              # LLM 区间 analyze/preview/status
│   │   │   ├── backtest.py                 # POST /api/backtest/run
│   │   │   ├── ws.py                       # WebSocket /ws
│   │   │   └── auth.py                     # require_api_key（可选；preview 可挂依赖）
│   │   ├── core/
│   │   │   ├── broker.py                   # BrokerGateway：quote(s)、candlesticks、下单
│   │   │   ├── market_calendar.py          # trade_day_for / is_trading_hours（US/HK 本地日）
│   │   │   ├── engine.py                   # flat/long/short 状态机
│   │   │   ├── risk.py                     # 日亏损、连续亏损；可注入 trade_day_provider
│   │   │   ├── backtest.py                 # 离线回测引擎
│   │   │   ├── notify.py                   # Server酱
│   │   │   └── credential_crypto.py        # RSA + AES-GCM（AUTO_TRADE_CREDENTIAL_KEY_PATH）
│   │   └── services/
│   │       ├── trade_execution_service.py  # 下单、pending 对账、tracked 入场成本持久化、HK tick
│   │       ├── strategy_service.py
│   │       ├── runtime_state_service.py
│   │       ├── daily_pnl_service.py
│   │       ├── credentials_service.py
│   │       ├── llm_advisor_service.py
│   │       ├── interval_application_service.py
│   │       ├── llm_interaction_service.py
│   │       ├── trade_event_service.py
│   │       └── data_aggregator.py          # 真实 K 线 → LLM prompt / ATR / 布林带
│   ├── tests/                              # pytest（见「测试约定」）
│   ├── alembic/                            # 历史迁移；运行时以 database._ensure_* 为准
│   ├── requirements.txt
│   └── requirements-dev.txt                # pytest / basedpyright
├── frontend/
│   ├── src/
│   │   ├── router/index.ts                 # /, /strategy, /history, /events, /backtest, /credentials
│   │   ├── api/                            # client.ts + 按域模块
│   │   ├── composables/                    # useDashboardData、useStatusStream…
│   │   ├── components/                     # PriceChart、PnLChart、BacktestChart
│   │   ├── views/                          # Dashboard、Strategy、TradeHistory、DecisionTimeline、Backtest、Credentials
│   │   └── types/index.ts
│   └── cypress/e2e/                        # E2E（stub API）
├── docker-compose.yaml                     # frontend 0.0.0.0:8080；backend 仅容器内网
├── docs/Roadmap.md
└── README.md                               # 完整 API 表与部署说明
```

## 构建 & 运行

### Docker（推荐，内网）

```bash
cp .env.example .env   # 填写 LONGPORT_*；API_KEY 可留空
docker compose up --build -d
# UI + API: http://<host>:8080  （/api、/ws 经 Nginx 反代）
```

### 后端（本地开发）

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate   # 必须 Python 3.11+
pip3 install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

### 前端（本地开发）

```bash
cd frontend
npm install
npm run dev        # http://localhost:3000，代理 /api、/ws → :8000
npm run type-check
npm run build
```

### 测试与类型检查

```bash
cd backend && python3 -m pytest tests/ -v
python3 -m basedpyright          # 需仓库根 pyrightconfig.json（本地可能 gitignore）
cd frontend && npm run type-check
```

## API 速查

完整说明见 `README.md`。主要前缀均为 `/api`（回测为 `/api/backtest`）。

| 域 | 代表路径 |
|----|----------|
| 策略 | `GET/PUT /api/strategy`，`GET /api/status`，`GET /api/status/history` |
| 控制 | `POST /api/control/{start,stop,pause,resume,kill-switch,disable-kill-switch}` |
| 交易 | `GET /api/orders`（`refresh=true` 强制同步今日订单），`POST /api/orders/{id}/cancel`，`GET /api/account`（批量 quote） |
| 事件 | `GET /api/events`，`GET /api/events/export` |
| 凭证 | `GET/PUT /api/credentials` |
| LLM | `POST /api/strategy/llm-interval/{preview,analyze}`，`GET …/status`，`PUT …/enable\|disable` |
| 回测 | `POST /api/backtest/run` |
| 实时 | `WS /ws` |
| 健康 | `GET /api/health` |

## 约束 & 注意事项

### Python 版本

**必须 3.11+**。`tests/test_ws.py` 在 3.9 下会因 `asyncio.Lock()` 离开 running loop 而失败。

### 测试约定（容易踩坑）

1. **每个测试文件用独立的 sqlite 文件**（`data/test_xxx.db` 或 `tmp_path`），在 `from app...` **之前**设置 `AUTO_TRADE_DATABASE_URL`。
2. **`conftest.py` 已设置**进程级 `AUTO_TRADE_CREDENTIAL_KEY_PATH`（临时 PEM），避免与开发机 `data/credential_private_key.pem`（可能带 KEK 加密）冲突。
3. **不要用 `os.environ.pop`**——改用 `monkeypatch.delenv`。
4. **`setup_class` 先 `drop_all` 再 `create_all`**；旧 DB 文件缺列会导致测试失败。
5. **`database.engine` 在 import 时绑定 `settings.database_url`**；测试中须在 import 前设 env，或 `monkeypatch` 替换 `database.engine`。

### 数据库迁移

- 运行时通过 `init_db()` → `_ensure_order_execution_columns`、`_ensure_order_raw_response_column`、`_ensure_strategy_config_llm_columns`、`_ensure_runtime_state_daily_pnl_date_column`、`_ensure_tracked_entries_table`、`_ensure_strategy_config_p4_columns`（P4 新增：`fee_rate_us`、`fee_rate_hk`、`min_repricing_pct`、`llm_action_cooldown_seconds`）补丁旧表。
- **`alembic/` 不用于生产**；加列须同步新增 `_ensure_*`。
- 首次补 `daily_pnl_date` 时会把 NULL 行的 `daily_pnl`/`consecutive_losses` 置 0（一次性）。

### 配置

- 业务 settings：`AUTO_TRADE_` 前缀（`app/config.py`）。
- 长桥凭据：`LONGPORT_*` 优先，兼容 `LONGBRIDGE_*`（`merge_longbridge_credentials`）。
- `CREDENTIAL_MASTER_KEY`：加密 DB 内凭据及 PEM；**轮换后旧密文不可解**。
- `AUTO_TRADE_CREDENTIAL_KEY_PATH`：RSA 私钥路径，默认 `data/credential_private_key.pem`。
- `DEEPSEEK_API_KEY`：LLM 顾问（无此前缀，见 config 的 `validation_alias`）。
- **API 鉴权增强不在 Roadmap 内**（owner decision 2026-05-25）：当前仅可信内网部署，`AUTO_TRADE_API_KEY` 可留空；若部署到不可信网络，必须重新评估所有写端点与 WebSocket 的访问控制。

### 交易执行（TradeExecutionService）

- **加权入场成本**保存在内存 + `tracked_entries` 表；`AppRunner` 启动时 `load_tracked_entries`，成交后 `persist_entry`；平仓 PnL 优先 tracked avg，不用 broker `avg_price`。
- HK 限价按港交所阶梯 tick 量化（`.HK`）；US 仍 0.01。
- 实盘下单后走 **pending 异步对账**（`_track_pending_order` + `_reconcile_pending_order`）；已删除阻塞式 `_wait_for_order_completion`。
- `_order_status_timeout_seconds` 默认 30；**`= 0` 表示禁用** reconcile 超时。超时后 pause、清 pending、恢复 engine snapshot。
- `execute()` 入口会再次 `risk.check()`，并拒绝已有 pending 时的并发下单。
- 券商已提交但 **DB 写入失败** → `OrderPersistenceError` 路径：尝试撤单、pause、回滚 snapshot。
- `engine.sync_state()` **不会**清空 `last_trigger_at`（保留冷却）。
- `StrategyConfig` 新增四项：`fee_rate_us` / `fee_rate_hk`（实盘普通平仓双边费用估算）、`min_repricing_pct`（LLM cancel-replace 最小改价阈值）、`llm_action_cooldown_seconds`（LLM 同方向发单冷却）。
- `_profit_guard_for_exit` 在原 `min_profit_amount` 校验上叠加 round-trip 费用：`expected_profit - estimated_fees < required_profit` 即 `ORDER_SKIPPED` 且 `payload.skip_category = "FEE"`；`allow_loss_exit=True` 完全绕过费用门槛。
- LLM `execute_llm_order_decision` 在调用 `cancel_pending_order` 之前执行改价（`REPRICING`）与冷却（`COOLDOWN`）gate；`CANCEL_PENDING` 不受 gate 影响。`_last_llm_action_at[(symbol, broker_side)]` 仅在 FILLED/SUBMITTED/PARTIAL_FILLED 后更新。
- `record_order_skipped` payload 现已稳定带 `skip_category` ∈ `{FEE, REPRICING, COOLDOWN, RISK, PENDING, POSITION}`；前端 `skipCategoryLabel` 是唯一渲染入口。

### 经纪商交互

- `BrokerGateway` 懒加载 `QuoteContext` / `TradeContext`。
- `get_quotes(symbols)` 批量报价；`get_candlesticks(symbol, period, count)` 供 `DataAggregator` / LLM。
- 单 symbol 行情订阅；RTH 内推送静默 ~90s 时 runner `_resubscribe_quotes_if_silent`；主动 `get_quote` 刷新设 `is_push=False` 以免误判推送存活。
- 测试通过 mock `BrokerGateway` 注入；`longport` 为可选依赖。

### 交易日历（market_calendar）

- `trade_day_for(market, instant)`：交易所本地**日历日**（非节假日历）；`AppRunner` 注入 `RiskController` 与 `DailyPnlService.to_trade_day`。
- `is_trading_hours`：周末 + RTH 窗口；用于行情重订守卫，**不**阻止盘前盘后下单。

### LLM 顾问

- 服务：`llm_advisor_service.py`、`interval_application_service.py`；HTTP：`api/llm_advisor.py`；后台：`main._llm_analysis_cron`。
- `DataAggregator` 拉真实日 K / 1 分钟 K；`LLMAdvisorService(broker=...)` 可复用 runner 的 gateway。
- LONG 态允许 LLM **降低** `buy_low`（追价加仓）；见 Roadmap P7'。
- 节流：`_LAST_ANALYSIS_TIMESTAMP` 为 `time.monotonic()`；判断前检查 `<= 0` 避免冷启动误判。

### AppRunner / lifespan

- `main.lifespan`：`asyncio.to_thread(runner.start, loop=...)` / `to_thread(runner.stop)`，避免阻塞事件循环。
- `sync_today_orders_from_broker` 约 15s 节流；`GET /api/orders?refresh=true` 可 `force` 同步。

### 代码风格

- 后端：`basedpyright` basic；仓库根 `python3 -m basedpyright` 目标 0 errors（需本地 `pyrightconfig.json`，该文件可能在 `.gitignore`）。
- 优先 `X | None`；泛型别名上下文若 PEP 604 报错则改用 `Optional[X]`。
- 提交前：`pytest`、`basedpyright`、`npm run type-check`。

### 文档

- 用户向说明以 **`README.md`** 为准；迭代计划见 **`docs/Roadmap.md`**。
- 修改 API/路由/部署行为时，同步更新 README 与本文件。
