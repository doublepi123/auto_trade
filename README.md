# Auto Trade

基于 [长桥 Longbridge](https://open.longbridge.com/) OpenAPI 的自动化区间交易系统。

## 架构

```
┌──────────────┐       ┌──────────────────┐
│   Vue 3 前端  │◄─REST─►│  FastAPI 后端     │
│   (Vite)     │◄─WS───►│                  │
└──────────────┘       │  ┌─────────────┐ │
                       │  │ 策略引擎     │ │
                       │  │ (状态机)    │ │
                       │  └──────┬──────┘ │
                       │         │        │
                       │  ┌──────▼──────┐ │
                       │  │  长桥 SDK   │ │
                       │  │ (行情+交易) │ │
                       │  └──────┬──────┘ │
                       │         │        │
                       │  ┌──────▼──────┐ │
                       │  │  风控模块   │ │
                       │  └──────┬──────┘ │
                       │         │        │
                       │  ┌──────▼──────┐ │
                       │  │  Server酱   │ │
                       │  │  通知推送   │ │
                       │  └─────────────┘ │
                       │                  │
                       │  ┌─────────────┐ │
                       │  │   SQLite    │ │
                       │  └─────────────┘ │
                       └──────────────────┘
```

数据流：`长桥 WebSocket 行情 → 策略引擎 → 风控判断 → 订单执行 → DB 存档 + Web UI 展示 + 通知推送`

## 功能

### 区间交易策略
- 设定最低价 `buy_low` 和最高价 `sell_high`
- 价格 ≤ 最低价 → 全仓买入（做多）或平空（回补）
- 价格 ≥ 最高价 → 全仓卖出（平多）或开空
- 可选启用做空：卖出后反向开空，触底回补
- 价格在区间内时观望，不操作
- 60 秒冷却期，防止阈值附近抖动触发连续下单

### 状态机

```
空仓(flat) ──价格≤buy_low──► 持仓(long) ──价格≥sell_high──► 空仓(flat)
     │                                                      │
     │                     (做空开启时)                       │
     └──价格≥sell_high──► 做空(short) ──价格≤buy_low──┘
```

### 风控
- 单日最大亏损限制（默认 $5000）
- 连续亏损 N 次自动暂停（默认 3 次）
- **按交易所本地日历日**切日（US 用美东、HK 用香港时间），避免 UTC 午夜误重置日盈亏与连损计数
- 手动暂停/恢复交易
- Kill Switch 紧急停止

### 盈亏与成本跟踪
- 加权入场成本（`tracked_entries`）持久化到 SQLite，进程重启后平仓 PnL 仍按系统记录的均价计算，不依赖券商可能滞后的 `avg_price`
- 启动时与券商持仓对账；数量偏差过大时写入 `TRACKED_ENTRY_DRIFT` 事件（决策时间线可见）

### 行情与运行时
- 长桥 WebSocket 推送行情；若 RTH 内推送静默超过约 90 秒，runner 自动退订并重订
- 推送中断时仍可通过每 15 秒主动 `get_quote` 续命；主动拉取不计入「推送活跃」检测
- FastAPI `lifespan` 在后台线程启动 runner，避免启动期阻塞 `/api/health`
- Runner 后台约每 15 秒将券商当日订单同步到本地库

### 通知
- 下单、成交、风控事件推送到 [Server酱](https://sct.ftqq.com/)

### 回测
- CSV 历史价格回测（`POST /api/backtest/run`），验证区间参数与风控规则
- Web UI **Backtest** 页：上传/粘贴 CSV、查看收益曲线与交易明细

### LLM 区间顾问（可选）
- DeepSeek 分析建议 `buy_low` / `sell_high`，支持预览与应用
- 可配置定时自动分析与区间调整
- Prompt 使用长桥真实日 K / 1 分钟 K（`BrokerGateway.get_candlesticks`），ATR(14) 与布林带基于历史 K 线计算
- 持仓状态下允许 LLM **下调** `buy_low`（追价加仓）或 **上抬** `sell_high`（抬高目标），见 `docs/Roadmap.md` P7'
- LLM 撤单重挂（`CANCEL_REPLACE`）仅在新旧价格差达到 `min_repricing_pct` 阈值时执行；否则保留原挂单并记录 `REPRICING` 跳过原因
- LLM 同方向（买/卖）发单受 `llm_action_cooldown_seconds` 独立冷却，未到期时记录 `COOLDOWN` 跳过原因；`CANCEL_PENDING` 撤单操作不受冷却影响

### 交易执行安全
- 普通平仓（非止损）在满足 `min_profit_amount` 之前，还需扣除按 `fee_rate_us` / `fee_rate_hk` 估算的双边手续费；费用后净收益仍不足时跳过并记录 `FEE` 原因
- 止损路径（`allow_loss_exit=True`）完全绕过费用门槛与改价/冷却限制，确保止损优先
- Decision Timeline 与 Dashboard 最近动作按分类展示跳过原因：`FEE` / `REPRICING` / `COOLDOWN` / `RISK` / `PENDING` / `POSITION`
- 回测引擎拥有独立的 `fee_rate` / `fixed_fee` / `slippage_pct`，不读取实盘的市场费率配置，离线模拟结果与实盘独立

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11+, FastAPI, SQLAlchemy 2.0, SQLite |
| 券商 SDK | longport (Longbridge Python SDK) |
| 前端 | Vue 3, Vite, Element Plus, TypeScript |
| 部署 | Docker Compose |

## 前置条件

- Docker 和 Docker Compose（推荐部署方式）
- 或 Python 3.11+ / Node.js 18+（本地开发）
- 长桥账户：需获取 App Key、App Secret、Access Token
- （可选）Server酱 SendKey：用于微信通知

## 快速开始

### 1. 配置凭证

```bash
cp .env.example .env
```

编辑 `.env`：

```env
LONGPORT_APP_KEY=你的AppKey
LONGPORT_APP_SECRET=你的AppSecret
LONGPORT_ACCESS_TOKEN=你的AccessToken
AUTO_TRADE_SCT_KEY=你的Server酱SendKey          # 可选
CREDENTIAL_MASTER_KEY=你的凭证加密主密钥           # 建议设置（Web UI 保存凭证前）
DEEPSEEK_API_KEY=你的DeepSeek密钥                # 可选，启用 LLM 区间顾问时需要
AUTO_TRADE_API_KEY=                              # 可选；内网部署可留空
AUTO_TRADE_FRONTEND_PORT=8080                    # Docker 前端监听端口（绑定 0.0.0.0）
```

长桥凭证获取：<https://open.longbridge.com/>

### 2. 启动服务

```bash
docker compose up --build -d
```

- 前端 Web UI（含 API 反向代理）: http://localhost:8080
- 健康检查: http://localhost:8080/api/health
- 局域网访问: `http://<本机IP>:8080`（Compose 将前端绑定到 `0.0.0.0`）

### 3. 配置策略

打开 Web UI → **Strategy** 页面，填写：

| 参数 | 说明 | 示例 |
|------|------|------|
| Symbol | 股票代码（格式：`CODE.MARKET`） | `AAPL.US` |
| Market | 市场 | `US` / `HK` |
| Buy Low Price | 触发买入的最低价 | `150.00` |
| Sell High Price | 触发卖出的最高价 | `200.00` |
| Short Selling | 是否启用做空 | `false` |
| Max Daily Loss | 单日最大亏损额度 | `5000` |
| Max Consecutive Losses | 连续亏损暂停阈值 | `3` |
| US Estimated Fee Rate (`fee_rate_us`) | 美股单边预估费率，用于实盘普通平仓的费用后收益门槛 | `0.05%` |
| HK Estimated Fee Rate (`fee_rate_hk`) | 港股单边预估费率，用于实盘普通平仓的费用后收益门槛 | `0.30%` |
| LLM Repricing Threshold (`min_repricing_pct`) | LLM 撤单重挂所需的最小改价百分比；未达阈值时保留原挂单 | `0.30%` |
| LLM Action Cooldown (`llm_action_cooldown_seconds`) | LLM 同方向（买/卖）发单的最小间隔；到期前跳过并记录原因 | `60s` |

保存后在 **Dashboard** 点击 **Start** 启动策略运行。

### 4. 停止服务

```bash
docker compose down
```

## 本地开发

### 前置要求

- Python 3.11+（与 `pyrightconfig.json` 中的 `pythonVersion` 保持一致；`tests/test_ws.py` 依赖 3.10+ 的 `asyncio` 行为）
- Node.js 18+

### 后端

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate  # 推荐使用 venv 锁定 Python 3.11+
pip3 install -r requirements.txt
pip3 install -r requirements-dev.txt  # LSP/type-check/test development dependencies
cp ../.env.example ../.env  # 或手动创建 backend/.env
uvicorn app.main:app --reload --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev     # http://localhost:3000，自动代理 /api 和 /ws 到后端
```

### 运行测试

```bash
cd backend
python3 -m pytest tests/ -v                     # 全部测试
python3 -m pytest tests/ --cov=app --cov-report=term  # 含覆盖率
python3 -m pytest tests/test_engine.py -v        # 单模块
```

---

## 项目结构

```
auto_trade/
├── backend/
│   ├── app/
│   │   ├── main.py                         # FastAPI 入口、lifespan、LLM 定时任务
│   │   ├── config.py                       # pydantic-settings（AUTO_TRADE_* / LONGPORT_*）
│   │   ├── database.py                     # 引擎、init_db、SQLite 列级迁移（_ensure_*）
│   │   ├── models.py                       # ORM：策略、订单、tracked_entries、事件、LLM、运行时状态等
│   │   ├── schemas.py                      # Pydantic 请求/响应
│   │   ├── runner.py                       # AppRunner：行情订阅、策略循环、WS 广播
│   │   ├── api/
│   │   │   ├── strategy.py                 # 策略配置、状态、状态历史
│   │   │   ├── trade.py                    # 订单、账户、事件、运行时控制
│   │   │   ├── credentials.py              # 加密凭证 CRUD
│   │   │   ├── llm_advisor.py              # LLM 区间分析与自动区间开关
│   │   │   ├── backtest.py                 # POST /api/backtest/run
│   │   │   ├── ws.py                       # WebSocket /ws
│   │   │   └── auth.py                     # API Key 依赖（可选，默认内网不启用）
│   │   ├── core/
│   │   │   ├── broker.py                   # 长桥 SDK（行情、K 线、批量 quote、下单、持仓）
│   │   │   ├── market_calendar.py          # US/HK 交易日与 RTH 判断（本地日历日）
│   │   │   ├── engine.py                   # 区间策略状态机
│   │   │   ├── risk.py                     # 日亏损、连续亏损、暂停、kill switch
│   │   │   ├── backtest.py                 # 离线回测引擎
│   │   │   ├── notify.py                   # Server酱通知
│   │   │   └── credential_crypto.py        # RSA + AES-GCM 凭证加密
│   │   └── services/
│   │       ├── strategy_service.py         # 策略与运行时状态 CRUD
│   │       ├── trade_execution_service.py  # 下单、pending 对账、tracked 入场成本、HK/US tick
│   │       ├── runtime_state_service.py    # 状态持久化与历史快照
│   │       ├── daily_pnl_service.py        # 订单账本日盈亏重算
│   │       ├── credentials_service.py      # 凭证存取（掩码响应）
│   │       ├── llm_advisor_service.py      # DeepSeek 区间建议
│   │       ├── interval_application_service.py  # 区间建议应用规则
│   │       ├── llm_interaction_service.py  # LLM 调用记录
│   │       ├── trade_event_service.py      # 决策时间线事件
│   │       └── data_aggregator.py          # LLM 行情聚合（真实 K 线、ATR/布林带）
│   ├── tests/                              # pytest
│   ├── alembic/                            # 历史迁移（运行时以 database._ensure_* 为准）
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── Dockerfile
│   └── docker-entrypoint.sh
├── frontend/
│   ├── src/
│   │   ├── App.vue                         # 布局与导航
│   │   ├── router/index.ts                 # Hash 路由（见下表）
│   │   ├── api/                            # 按域拆分的 HTTP 客户端
│   │   │   ├── client.ts                   # axios 实例
│   │   │   ├── strategy.ts / trade.ts / credentials.ts
│   │   │   ├── llm_advisor.ts / backtest.ts / events.ts
│   │   ├── composables/                    # useDashboardData、useStatusStream 等
│   │   ├── components/                     # PriceChart、PnLChart、BacktestChart
│   │   ├── types/index.ts
│   │   └── views/
│   │       ├── Dashboard.vue               # 实时面板、图表、启停控制
│   │       ├── Strategy.vue                # 策略参数 + LLM 区间
│   │       ├── TradeHistory.vue            # 订单列表与撤单
│   │       ├── DecisionTimeline.vue        # 决策事件时间线（/events）
│   │       ├── Backtest.vue                # CSV 回测
│   │       └── Credentials.vue             # 凭证配置（不回显明文）
│   ├── cypress/e2e/                        # E2E（navigation、dashboard、backtest、events…）
│   ├── nginx.conf                          # 生产：/api、/ws 反代到 backend
│   ├── vite.config.ts
│   └── Dockerfile
├── docker-compose.yaml                     # frontend 0.0.0.0:8080，backend 仅内网
├── docs/Roadmap.md
├── .env.example
└── README.md
```

### 前端路由

| 路径 | 页面 |
|------|------|
| `/#/` | Dashboard — 实时状态、图表、启停/暂停/kill switch |
| `/#/strategy` | Strategy — 区间参数、LLM 顾问 |
| `/#/history` | Trade History — 今日/历史订单、撤单；今日列表默认读本地库，点「刷新」时 `refresh=true` 强制同步券商 |
| `/#/events` | Decision Timeline — 交易与 LLM 决策事件 |
| `/#/backtest` | Backtest — CSV 回测 |
| `/#/credentials` | Credentials — 长桥 / Server酱凭证 |

## API 参考

除特别说明外，路径均相对于 Web 入口（Docker 下为 `http://<host>:8080`；本地开发前端代理到 `localhost:8000`）。

### 健康与策略

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | 健康检查（`ok`, `env`） |
| `GET` | `/api/strategy` | 获取策略配置 |
| `PUT` | `/api/strategy` | 更新策略配置（支持部分字段） |
| `GET` | `/api/status` | 运行时状态：引擎、价格、盈亏、暂停、kill switch、`runner_running` |
| `GET` | `/api/status/history` | 状态历史快照；查询参数 `from`、`to`、`limit`（默认近 6 小时，最多 1000 条） |

### 凭证与账户

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/credentials` | 凭证配置状态（掩码，无明文） |
| `PUT` | `/api/credentials` | 更新长桥 / Server酱凭证 |
| `GET` | `/api/account` | 账户净值、现金、持仓（持仓市值一次批量 `get_quotes`） |

### 订单与事件

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/orders` | 分页订单；`scope=today\|history`，`page`，`page_size`（或兼容 `limit`）；`refresh=true` 时（仅 `scope=today`）先强制从券商同步再返回本地库 |
| `POST` | `/api/orders/{order_id}/cancel` | 撤销指定券商订单 |
| `GET` | `/api/events` | 决策时间线分页；`page`，`page_size`，可选 `symbol`、`event_type` |
| `GET` | `/api/events/export` | 导出事件；`format=csv\|json`，`limit` |

### 运行时控制

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/control/start` | 启动策略（订阅行情） |
| `POST` | `/api/control/stop` | 停止策略 |
| `POST` | `/api/control/pause` | 暂停交易；可选 JSON body：`reason` |
| `POST` | `/api/control/resume` | 恢复交易 |
| `POST` | `/api/control/kill-switch` | 紧急停止 |
| `POST` | `/api/control/disable-kill-switch` | 解除紧急停止 |

### LLM 区间顾问

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/strategy/llm-interval/preview` | 预览分析（不应用、不下单） |
| `POST` | `/api/strategy/llm-interval/analyze` | 分析并可应用区间 / 触发 LLM 下单建议 |
| `GET` | `/api/strategy/llm-interval/status` | 当前 LLM 区间状态与最近建议 |
| `GET` | `/api/strategy/llm-interval/interactions` | 历史交互记录；`limit` |
| `PUT` | `/api/strategy/llm-interval/enable` | 开启自动定时分析 |
| `PUT` | `/api/strategy/llm-interval/disable` | 关闭自动定时分析 |

### 回测

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/backtest/run` | 运行回测；body：`csv_text` 或 `price_points[]` + `params` |

### WebSocket

| Protocol | Path | Description |
|----------|------|-------------|
| `WS` | `/ws` | 实时推送引擎状态、价格、风控标志等 JSON |

> **说明：** `POST /api/strategy/llm-interval/preview` 在配置了 `AUTO_TRADE_API_KEY` 且非 dev/test 时可要求 `X-API-Key`；内网部署通常留空该变量即可。

## 配置参考

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `AUTO_TRADE_ENV` | 运行环境 | `dev` |
| `AUTO_TRADE_DATABASE_URL` | SQLite 数据库路径 | `sqlite:///data/auto_trade.db` |
| `AUTO_TRADE_FRONTEND_PORT` | Docker 前端映射端口（绑定 `0.0.0.0`） | `8080` |
| `LONGPORT_APP_KEY` | 长桥 App Key | - |
| `LONGPORT_APP_SECRET` | 长桥 App Secret | - |
| `LONGPORT_ACCESS_TOKEN` | 长桥 Access Token | - |
| `AUTO_TRADE_SCT_KEY` | Server酱 SendKey | - |
| `AUTO_TRADE_API_KEY` | 可选 API 密钥（内网可留空） | - |
| `CREDENTIAL_MASTER_KEY` | 凭证加密主密钥；保护 `data/credential_private_key.pem` | - |
| `AUTO_TRADE_CREDENTIAL_KEY_PATH` | RSA 私钥文件路径（测试/多实例可覆盖） | `data/credential_private_key.pem` |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥（LLM 顾问） | - |
| `DEEPSEEK_MODEL` | LLM 模型 ID | `deepseek-v4-pro` |
| `DEEPSEEK_REASONING_EFFORT` | Thinking 力度（`high` / `max`） | `max` |
| `DEEPSEEK_THINKING_TYPE` | 是否启用 thinking（`enabled` / `disabled`） | `enabled` |
| `AUTO_TRADE_MIN_EXIT_PROFIT_PCT` | 平仓最低盈利百分比缓冲 | `0.2` |

长桥 SDK 官方使用 `LONGPORT_*` 凭证变量；项目仍兼容旧的 `LONGBRIDGE_*` 变量名。`auto_trade` 业务配置使用 `AUTO_TRADE_` 前缀；`CREDENTIAL_MASTER_KEY` 无此前缀。

## 安全

- **绝不提交** `.env`、API 凭证到代码仓库（已加入 `.gitignore`）
- 长桥凭证可通过环境变量注入，也可通过加密的凭证 API / Web UI 保存到本地；前端不会回显或输出明文凭证
- **部署假设：仅在内网使用。** Docker Compose 将前端绑定到 `0.0.0.0:${AUTO_TRADE_FRONTEND_PORT:-8080}`，便于局域网内其它设备访问；请确保网络边界可信（防火墙/VLAN），不要将服务直接暴露到公网
- `AUTO_TRADE_API_KEY` 为可选配置项（内网部署可不启用 API 鉴权）
- 生产环境建议通过 Docker secrets 或外部密钥管理服务注入敏感信息

## 限制

- 仅支持单标的策略，不支持组合交易
- 长桥 SDK 不适合高频交易，请控制下单频率
- 行情权限、交易权限和可交易品种以长桥账户实际开通为准
- 交易时段、费率以长桥 App 和官网实时展示为准
- `market_calendar` 按交易所**本地日历日**与周末 RTH 判断，**不含节假日历**；休市日仍可能计入当日风控窗口
- 今日订单列表默认读 SQLite；Dashboard 最近订单最多落后 runner 后台同步间隔（约 15s），需最新数据请在订单页点「刷新」
- 回测为离线 CSV 模拟，与实盘滑点/成交可能有差异

## License

MIT
