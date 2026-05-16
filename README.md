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
- 手动暂停/恢复交易
- Kill Switch 紧急停止

### 通知
- 下单、成交、风控事件推送到 [Server酱](https://sct.ftqq.com/)

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
AUTO_TRADE_SCT_KEY=你的Server酱SendKey  # 可选
```

长桥凭证获取：<https://open.longbridge.com/>

### 2. 启动服务

```bash
docker compose up --build -d
```

- 前端 Web UI: http://localhost:8080
- 后端 API: http://localhost:8000
- 健康检查: http://localhost:8000/api/health

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

保存后在 **Dashboard** 点击 **Start** 启动策略运行。

### 4. 停止服务

```bash
docker compose down
```

## 本地开发

### 后端

```bash
cd backend
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp ../.env.example ../.env  # 或手动创建 backend/.env
.venv/bin/uvicorn app.main:app --reload --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev     # http://localhost:3000，自动代理 /api 和 /ws 到后端
```

### 重置本地开发数据

本项目当前不维护 SQLite 迁移。重构或模型变更后，如遇到旧数据导致的异常，可以停止服务并删除 `backend/data/auto_trade.db` 或 `.env` 中 `AUTO_TRADE_DATABASE_URL` 指向的 SQLite 文件，然后重新启动服务。

### 运行测试

```bash
cd backend
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/ --cov=app --cov-report=term  # 含覆盖率
.venv/bin/python -m pytest tests/test_engine.py -q        # 单模块
```

---

## 项目结构

```
auto_trade/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口，lifespan 管理
│   │   ├── config.py            # pydantic-settings 配置
│   │   ├── database.py          # SQLAlchemy 引擎 / 会话 / init_db
│   │   ├── models.py            # ORM 模型 (StrategyConfig, OrderRecord, RiskEvent, RuntimeState)
│   │   ├── schemas.py           # Pydantic 请求/响应 schema
│   │   ├── runner.py            # 后台运行器：订阅行情 → 策略评估 → 下单 → 广播
│   │   ├── api/
│   │   │   ├── credentials.py   # GET/PUT /api/credentials
│   │   │   ├── strategy.py      # GET/PUT /api/strategy, GET /api/status
│   │   │   ├── trade.py         # GET /api/orders, /api/account, POST /api/control/*
│   │   │   └── ws.py            # WebSocket /ws（ConnectionManager 单例）
│   │   ├── core/
│   │   │   ├── broker.py        # 长桥 SDK 封装（Quote/Trade/Position）
│   │   │   ├── engine.py        # 策略引擎（flat/long/short 状态机）
│   │   │   ├── risk.py          # 风控控制器（日亏损、连续亏损、暂停、kill switch）
│   │   │   └── notify.py        # Server酱通知客户端
│   │   └── services/
│   │       ├── account_service.py          # 账户摘要、现金余额、持仓读取
│   │       ├── credentials_service.py      # 凭证读取、保存与清除
│   │       ├── runtime_state_service.py    # 运行时状态持久化
│   │       ├── strategy_service.py         # 策略配置 CRUD
│   │       └── trade_execution_service.py  # 交易控制与订单执行
│   ├── tests/                   # pytest 单元测试与覆盖率
│   ├── requirements.txt
│   ├── Dockerfile
│   └── docker-entrypoint.sh
├── frontend/
│   ├── src/
│   │   ├── App.vue              # 容器布局 + 导航
│   │   ├── router/index.ts      # 路由：/ → Dashboard, /strategy, /history
│   │   ├── api/                 # axios 客户端与 credentials/strategy/trade 接口模块
│   │   ├── composables/         # useDashboardData/useStatusStream/useFormSaveState
│   │   ├── types/index.ts       # TypeScript 类型定义
│   │   └── views/
│   │       ├── Credentials.vue   # 长桥与 Server酱凭证管理
│   │       ├── Dashboard.vue     # 实时状态面板 + 启停/暂停/恢复/kill switch
│   │       ├── Strategy.vue      # 策略参数配置表单
│   │       └── TradeHistory.vue  # 订单成交历史表格
│   ├── package.json
│   ├── vite.config.ts
│   ├── nginx.conf               # Nginx 反向代理配置（生产）
│   └── Dockerfile
├── docker-compose.yaml
├── .env.example
├── .gitignore
└── README.md
```

## API 参考

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/credentials` | 获取凭证配置状态 |
| `PUT` | `/api/credentials` | 保存或清除凭证 |
| `GET` | `/api/strategy` | 获取策略配置 |
| `PUT` | `/api/strategy` | 更新策略配置 |
| `GET` | `/api/status` | 获取运行时状态（引擎状态、价格、盈亏） |
| `GET` | `/api/account` | 获取账户资产、现金和持仓摘要 |
| `GET` | `/api/orders` | 查询订单列表（`?limit=50`） |
| `POST` | `/api/control/start` | 启动策略运行 |
| `POST` | `/api/control/stop` | 停止策略运行 |
| `POST` | `/api/control/pause` | 暂停交易 |
| `POST` | `/api/control/resume` | 恢复交易 |
| `POST` | `/api/control/kill-switch` | 紧急停止 |
| `POST` | `/api/control/disable-kill-switch` | 关闭紧急停止 |
| `WS` | `/ws` | WebSocket 实时状态推送 |

## 配置参考

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `AUTO_TRADE_ENV` | 运行环境 | `dev` |
| `AUTO_TRADE_DATABASE_URL` | SQLite 数据库路径 | `sqlite:///data/auto_trade.db` |
| `LONGPORT_APP_KEY` | 长桥 App Key | - |
| `LONGPORT_APP_SECRET` | 长桥 App Secret | - |
| `LONGPORT_ACCESS_TOKEN` | 长桥 Access Token | - |
| `AUTO_TRADE_SCT_KEY` | Server酱 SendKey | - |

长桥 SDK 官方使用 `LONGPORT_*` 凭证变量；项目仍兼容旧的 `LONGBRIDGE_*` 变量名。所有 auto_trade 特定配置均使用 `AUTO_TRADE_` 前缀。

## 安全

- **绝不提交** `.env`、API 凭证到代码仓库（已加入 `.gitignore`）
- 长桥凭证通过环境变量注入，不写入配置文件
- 生产环境建议通过 Docker secrets 或外部密钥管理服务注入敏感信息
- Web UI 默认绑定 `127.0.0.1`，如暴露到公网需配置反向代理 + HTTPS + 认证

## 限制

- 仅支持单标的策略，不支持组合交易
- 长桥 SDK 不适合高频交易，请控制下单频率
- 行情权限、交易权限和可交易品种以长桥账户实际开通为准
- 交易时段、费率以长桥 App 和官网实时展示为准
- 暂不支持回测系统

## License

MIT
