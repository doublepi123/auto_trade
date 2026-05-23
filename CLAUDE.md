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
│   │   ├── database.py                     # engine、init_db、SQLite _ensure_* 列补丁
│   │   ├── models.py                       # StrategyConfig、OrderRecord、TradeEvent、LLMInteraction、RuntimeState…
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
│   │   │   ├── broker.py                   # BrokerGateway（longport）
│   │   │   ├── engine.py                   # flat/long/short 状态机
│   │   │   ├── risk.py                     # 日亏损、连续亏损、暂停、kill switch
│   │   │   ├── backtest.py                 # 离线回测引擎
│   │   │   ├── notify.py                   # Server酱
│   │   │   └── credential_crypto.py        # RSA + AES-GCM（AUTO_TRADE_CREDENTIAL_KEY_PATH）
│   │   └── services/
│   │       ├── trade_execution_service.py  # 下单、pending 对账、持久化失败恢复
│   │       ├── strategy_service.py
│   │       ├── runtime_state_service.py
│   │       ├── daily_pnl_service.py
│   │       ├── credentials_service.py
│   │       ├── llm_advisor_service.py
│   │       ├── interval_application_service.py
│   │       ├── llm_interaction_service.py
│   │       ├── trade_event_service.py
│   │       └── data_aggregator.py
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
| 交易 | `GET /api/orders`，`POST /api/orders/{id}/cancel`，`GET /api/account` |
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

- 运行时通过 `init_db()` → `_ensure_order_execution_columns`、`_ensure_order_raw_response_column`、`_ensure_strategy_config_llm_columns`、`_ensure_runtime_state_daily_pnl_date_column` 补丁旧表。
- **`alembic/` 不用于生产**；加列须同步新增 `_ensure_*`。
- 首次补 `daily_pnl_date` 时会把 NULL 行的 `daily_pnl`/`consecutive_losses` 置 0（一次性）。

### 配置

- 业务 settings：`AUTO_TRADE_` 前缀（`app/config.py`）。
- 长桥凭据：`LONGPORT_*` 优先，兼容 `LONGBRIDGE_*`（`merge_longbridge_credentials`）。
- `CREDENTIAL_MASTER_KEY`：加密 DB 内凭据及 PEM；**轮换后旧密文不可解**。
- `AUTO_TRADE_CREDENTIAL_KEY_PATH`：RSA 私钥路径，默认 `data/credential_private_key.pem`。
- `DEEPSEEK_API_KEY`：LLM 顾问（无此前缀，见 config 的 `validation_alias`）。

### 交易执行（TradeExecutionService）

- 实盘下单后走 **pending 异步对账**（`_track_pending_order` + `_reconcile_pending_order`），非阻塞 `_wait_for_order_completion`。
- `_order_status_timeout_seconds` 默认 30；**`= 0` 表示禁用** reconcile 超时。超时后 pause、清 pending、恢复 engine snapshot。
- `execute()` 入口会再次 `risk.check()`，并拒绝已有 pending 时的并发下单。
- 券商已提交但 **DB 写入失败** → `OrderPersistenceError` 路径：尝试撤单、pause、回滚 snapshot。
- `engine.sync_state()` **不会**清空 `last_trigger_at`（保留冷却）。

### 经纪商交互

- `BrokerGateway` 懒加载 `QuoteContext` / `TradeContext`。
- 单 symbol 行情订阅；换 symbol 会 unsubscribe 旧的。
- 测试通过 mock `BrokerGateway` 注入；`longport` 为可选依赖。

### LLM 顾问

- 服务：`llm_advisor_service.py`、`interval_application_service.py`；HTTP：`api/llm_advisor.py`；后台：`main._llm_analysis_cron`。
- 节流：`_LAST_ANALYSIS_TIMESTAMP` 为 `time.monotonic()`；判断前检查 `<= 0` 避免冷启动误判。

### 代码风格

- 后端：`basedpyright` basic；仓库根 `python3 -m basedpyright` 目标 0 errors（需本地 `pyrightconfig.json`，该文件可能在 `.gitignore`）。
- 优先 `X | None`；泛型别名上下文若 PEP 604 报错则改用 `Optional[X]`。
- 提交前：`pytest`、`basedpyright`、`npm run type-check`。

### 文档

- 用户向说明以 **`README.md`** 为准；迭代计划见 **`docs/Roadmap.md`**。
- 修改 API/路由/部署行为时，同步更新 README 与本文件。
