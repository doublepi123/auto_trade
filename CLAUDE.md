# CLAUDE.md

本文件用于指导 Claude Code 在本仓库内的工作方式。**优先级高于 `~/code/CLAUDE.md`（那份描述的是另一个项目，不适用于此处）。**

## 项目概述

`auto_trade` 是基于长桥（Longbridge / Longport）OpenAPI 的自动化**区间交易**系统：

- 后端：FastAPI + SQLAlchemy 2.0 + SQLite，运行交易状态机、风控、订单执行、Server酱通知。
- 前端：Vue 3 + Vite + Element Plus + TypeScript。
- 经纪商 SDK：`longport`（行情订阅 + 下单）。
- 可选：DeepSeek LLM 顾问，定期建议 `buy_low` / `sell_high` 区间。

## 目录结构

```
backend/
├── app/
│   ├── main.py                          # FastAPI 入口
│   ├── config.py                        # pydantic-settings (env_prefix=AUTO_TRADE_)
│   ├── database.py                      # engine / SessionLocal / init_db + 列级自动迁移
│   ├── models.py                        # SQLAlchemy 模型（StrategyConfig, RuntimeState, OrderRecord, RiskEvent, CredentialConfig）
│   ├── schemas.py                       # Pydantic API schemas
│   ├── runner.py                        # AppRunner: 后台策略循环线程
│   ├── api/                             # HTTP/WS 路由
│   │   ├── auth.py                      # require_api_key 依赖
│   │   ├── credentials.py               # 加密凭据 CRUD
│   │   ├── strategy.py                  # 策略配置/启停
│   │   ├── trade.py                     # 订单/账户
│   │   ├── ws.py                        # WebSocket 推送
│   │   └── llm_advisor.py               # LLM 区间建议
│   ├── core/
│   │   ├── broker.py                    # BrokerGateway：封装 longport SDK
│   │   ├── engine.py                    # EngineState 状态机
│   │   ├── risk.py                      # 风控规则
│   │   ├── notify.py                    # Server酱通知
│   │   └── credential_crypto.py         # RSA 加密 + KEK
│   └── services/                        # 业务逻辑层
├── tests/                               # pytest 套件（注意见“测试约定”）
├── alembic/                             # 历史迁移；运行时实际依赖 database.py 里的 _ensure_* 列补丁
├── requirements.txt
└── requirements-dev.txt                 # pytest / basedpyright
frontend/                                # Vue 3 应用
docs/                                    # 设计文档
```

## 构建 & 运行

### 后端

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate   # 必须使用 Python 3.11+
pip3 install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev        # http://localhost:3000
npm run type-check # vue-tsc --noEmit
npm run build      # 生产构建
```

### 测试与类型检查

```bash
# 后端测试
cd backend && python3 -m pytest tests/ -v

# 后端类型检查（仓库根）
python3 -m basedpyright

# 前端类型检查
cd frontend && npm run type-check
```

## 约束 & 注意事项

### Python 版本
**必须 3.11+**。`pyrightconfig.json` 声明 `pythonVersion=3.11`；`tests/test_ws.py::test_init_empty_connections` 在 3.9 下会因为 `asyncio.Lock()` 离开 running loop 而失败。

### 测试约定（容易踩坑）
1. **每个测试文件用独立的 sqlite 文件**（`data/test_xxx.db`），通过模块顶部 `os.environ["AUTO_TRADE_DATABASE_URL"] = ...` 设置，必须在 `from app...` 之前。
2. **不要在测试里用 `os.environ.pop`**——会污染进程环境，破坏后续按字母序运行的测试。改用 `monkeypatch.delenv`。
3. **`setup_class` 必须先 `Base.metadata.drop_all` 再 `create_all`**。`create_all` 单独使用时不会给已存在的表加新列，旧 DB 文件会让新加字段的测试失败。
4. **`app/database.py` 的 `engine` 在模块导入时就绑定 `settings.database_url`**。测试如果想用自己的 DB，要么在 import 前设 env var，要么自己 `create_engine`，**不要**指望 setup_class 里改 env var 还能生效。

### 数据库迁移
- 已存在的 SQLite schema 通过 `app/database.py` 里的 `_ensure_order_execution_columns` / `_ensure_strategy_config_llm_columns` / `_ensure_runtime_state_daily_pnl_date_column` 在 `init_db()` 时做列级补丁。
- `alembic/` 目录是历史产物，**生产实际不跑 alembic**。加字段记得同步更新 `_ensure_*_columns`。

### 配置
- 所有 settings 走环境变量，前缀 `AUTO_TRADE_`（见 `app/config.py`）。
- 长桥凭据兼容三种命名：`AUTO_TRADE_LONGBRIDGE_*` / `LONGPORT_*` / `LONGBRIDGE_*`（顺序匹配，参考 `merge_longbridge_credentials`）。
- `CREDENTIAL_MASTER_KEY` 用于加密用户填入的 SCT key 等凭据；**修改后旧密文不可解**。

### 经纪商交互
- `BrokerGateway` 在第一次需要时通过 `_init_clients()` 懒加载 `QuoteContext` / `TradeContext`。
- 行情订阅是单 symbol 的（切换 symbol 时会 unsubscribe 旧的）。
- `longport` SDK 是可选依赖；测试里通过 mock 注入。

### LLM 顾问
- 入口：`app/services/llm_advisor_service.py`、`app/services/interval_application_service.py`。
- 节流：`_LAST_ANALYSIS_TIMESTAMP` 是 `time.monotonic()` 时间戳，进程级全局；判 throttle 前要检查 `<= 0`，避免冷启动误判。

### 代码风格
- 后端遵守 `basedpyright basic` 模式，仓库根 `python3 -m basedpyright` 必须 0 errors。
- 优先用 `X | None`；如果出现在 `tuple[...]` 类型别名里且 pyright 报错（PEP 604 在某些泛型上下文下解析有差异），改用 `Optional[X]`。
- 提交前自行运行：`python3 -m basedpyright`、`cd backend && pytest`、`cd frontend && npm run type-check`。
