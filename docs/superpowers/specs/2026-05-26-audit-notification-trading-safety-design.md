# P5+: 操作审计 + 多渠道报警 + 交易可靠性补强 设计

> **日期：** 2026-05-26（评审修订）
> **代号：** P5+（Roadmap 原 P5 与审计遗留 #9、#10 合并）
> **基线：** `pytest 435 passed`，`basedpyright` 0 errors / 0 warnings
> **目标分支：** `main`
> **前置阅读：** `docs/Roadmap.md`、`docs/superpowers/specs/2026-05-25-trade-execution-safety-design.md`

## 1. 背景与动机

P4（2026-05-25 交付）补完了费用门槛、LLM 改价/冷却 gate、跳过原因分类。下一组紧迫问题在两条线上：

1. **运维侧（Roadmap P5）**：策略修改 / 启停 / Kill Switch / 凭证修改 / 手动撤单**没有任何审计记录**；通知通道只有 Server 酱单点，挂了就静默。
2. **审计遗留**：
   - **#9** 交易时段守卫——`is_trading_hours` 已实现，但只用在 `runner.py:646` 行情重订阅看门狗里，**没有拦截下单**。盘前/盘后报价仍会触发交易。
   - **#10** 券商调用无 retry/backoff——瞬时限流靠 `_is_auto_resumable_pause_reason` 字符串匹配（含中文 `限流` `频率`）兜底，吸收能力弱。

这两条审计项与 P5 的事件源天然耦合：交易时段守卫、broker 重试本身就是审计要记录的事件，分两轮上线会重复改一遍 Notifier 链路。本迭代合并交付。

## 2. 范围与切除

### 范围（Task 拆分）

| Task | 主题 | 主要交付 | 依赖 |
|------|------|----------|------|
| **T1** | AuditLog 基础设施 | `audit_logs` 表 + `_ensure_audit_log_table` + `AuditLogger` 工具类 + 写端点接入 | — |
| **T2** | 交易时段守卫 | `StrategyConfig.trading_session_mode`（默认 `ANY`）+ `AppRunner` 撤单前 gate + `TradeExecutionService.execute` 二次 gate + `skip_category="SESSION"` + AuditLog | T1 |
| **T3** | Broker retry/backoff | `BrokerGateway._call_with_retry`（订单全量退避 / 行情轻量）；longport 异常类型优先；`BROKER_RETRY` 审计 | T1 |
| **T4** | Notifier 抽象 + Webhook + 分级 | `NotifierInterface`，`MultiChannelNotifier`，`WebhookNotifier`；`notify_risk_event` 带 severity | T1 |
| **T5** | 前端集成 | Credentials 通知渠道、Strategy 交易时段、Timeline `source` + 多选筛选、审计卡片 | T1–T4 |
| **T6** | 测试 | pytest（~30 新增）+ Cypress（3 新增 spec）+ basedpyright 清零 P8' 遗留 | 全部 |

### 显式 YAGNI 切除

- ❌ 独立审计 UI 页面 —— 复用 Decision Timeline 筛选。
- ❌ Webhook 模板编辑器 —— 固定 JSON schema `{title, content, severity, timestamp, action?}`。
- ❌ 通知重发队列 —— 单次失败仅 log warning，不持久化重发。
- ❌ **审计日志导出** —— 首版 `GET /api/events/export` **仍仅导出 `trade_events`**；审计行只在 Timeline 分页查询可见。
- ❌ 审计独立查询 API —— 复用 `GET /api/events`。
- ❌ 交易所节假日日历 —— `RTH_ONLY` 仅周末 + 常规 RTH 时段（与现有 `is_trading_hours` 一致）；UI 需提示非完整交易日历。

## 3. 数据模型

> **命名约定：** 与仓库一致——凭证表模型为 `CredentialConfig`（`credential_config`），Server 酱字段为 **`sct_key`**（非 `serverchan_sct_key`）。`StrategyConfig` 上另有历史字段 `sct_key`，本迭代通知只读 **`CredentialConfig.sct_key`**。

### 3.1 新表：`audit_logs`

```python
class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="INFO")
    actor_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="anonymous")
    source_ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    request_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="SUCCESS")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow, index=True)
```

- **`action` 枚举**：`START / STOP / PAUSE / RESUME / KILL_SWITCH / DISABLE_KILL_SWITCH / STRATEGY_UPDATE / CREDENTIALS_UPDATE / ORDER_CANCEL / TRADING_SESSION_BLOCKED / BROKER_RETRY`
- **`severity` 枚举**：`INFO / WARNING / CRITICAL`
- **`actor_hash`**：请求头 `X-API-Key` 做 SHA-256，取 digest 前 16 字节 hex；无 header → `anonymous`
- **`source_ip`**：`X-Forwarded-For` 第一段或 `request.client.host`
- **`request_summary`**：dict 自动 `json.dumps`；超过 `AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT`（默认 2048）字节截断 + `...truncated`
- **`result`**：`SUCCESS / FAILED / SKIPPED`
- **索引**：`created_at`、`action`

### 3.2 `StrategyConfig` 新字段

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `trading_session_mode` | `String(16)` | `"ANY"` | `RTH_ONLY` 仅 RTH 内允许**新下单**；`ANY` 兼容现行为 |

**默认值 `ANY`**：上线零行为变更；用户主动切 `RTH_ONLY` 才生效。

### 3.3 `CredentialConfig` 新字段

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `notification_channels` | `Text`（JSON） | `'[{"type":"serverchan","severity_floor":"INFO"}]'` | 多渠道配置 |

**schema**：

```json
[
  {"type": "serverchan", "severity_floor": "INFO"},
  {"type": "webhook", "url": "https://...", "severity_floor": "WARNING"}
]
```

- `severity_floor`：该渠道只接收 severity ≥ floor 的通知
- 现有 **`sct_key`** 列保留；`_ensure_*` 回填时默认 channel 指向 `sct_key`
- Webhook URL 明文存（内网/非券商凭证）

### 3.4 运行时迁移补丁

`init_db()` 顺序、幂等：

1. `_ensure_audit_log_table`
2. `_ensure_strategy_config_session_columns`（独立于 P4 `trade_safety_columns`）
3. `_ensure_credential_config_notification_channels_column`（缺值回填默认 JSON）

`alembic/` 不动。

## 4. 后端架构

### 4.1 `AuditLogger`（`backend/app/core/audit.py`）

```python
class AuditLogger:
    def __init__(self, session_factory: Callable[[], Session]) -> None: ...

    def record(
        self,
        action: str,
        *,
        severity: str = "INFO",
        actor_hash: str = "anonymous",
        source_ip: str = "",
        request_summary: dict | str = "",
        result: str = "SUCCESS",
    ) -> None: ...  # 失败仅 log warning，不抛

    @staticmethod
    def hash_actor(api_key: str | None) -> str: ...

    @staticmethod
    def extract_ip(request: Request) -> str: ...
```

- 同步写入；脱敏在**调用站**（handler），不在 logger 内。

### 4.2 API 接入点

| 端点 | action | severity | request_summary |
|------|--------|----------|-----------------|
| `POST /api/control/start` | `START` | `INFO` | `{}` |
| `POST /api/control/stop` | `STOP` | `INFO` | `{}` |
| `POST /api/control/pause` | `PAUSE` | `INFO` | `{reason}` |
| `POST /api/control/resume` | `RESUME` | `INFO` | `{}` |
| `POST /api/control/kill-switch` | `KILL_SWITCH` | `CRITICAL` | `{reason}` |
| `POST /api/control/disable-kill-switch` | `DISABLE_KILL_SWITCH` | `WARNING` | `{}` |
| `PUT /api/strategy` | `STRATEGY_UPDATE` | `INFO` | diff（仅 changed keys） |
| `PUT /api/credentials` | `CREDENTIALS_UPDATE` | `INFO` | 脱敏后 payload |
| `POST /api/orders/{id}/cancel` | `ORDER_CANCEL` | `INFO` | `{symbol, quantity, side}` |

**`result` 规则（成功与失败都记）：**

- HTTP 2xx 且业务意图达成 → `SUCCESS`
- HTTP 4xx/5xx 或业务拒绝（如 Kill Switch 已开仍 start → 403）→ `FAILED`，`request_summary` 含 `detail`（来自 `HTTPException.detail`）
- 依赖注入：`get_audit_logger()`、`extract_actor(request)`；handler 用 `try/finally` 或统一装饰器，**无论抛不抛异常都写一行**（失败在 `except HTTPException` 分支设 `FAILED`）

**`actor_hash` 预期（P2 不做前提下）：**

- 当前仅 `POST /strategy/llm-interval/preview` 挂 `require_api_key()`；control / strategy / credentials / cancel **无鉴权**
- 未配置 `AUTO_TRADE_API_KEY` 或请求无 header 时，运维审计主要靠 **`source_ip` + 时间 + action`**，`actor_hash` 多为 `anonymous`——在运维文档中说明，不阻塞本迭代

**脱敏 allowlist：**

- `STRATEGY_UPDATE`：策略数值字段明文（含 `trading_session_mode` 等）
- `CREDENTIALS_UPDATE`：`longbridge_*` / `encrypted_*` / **`sct_key`** / `webhook.url` → `***`；保留 `notification_channels[*].type`、`severity_floor`

**`STRATEGY_UPDATE` diff 实现（`StrategyService.update_config`）：**

```python
before = {k: getattr(config, k) for k in STRATEGY_AUDIT_KEYS}
# ... apply payload ...
after = {k: getattr(config, k) for k in STRATEGY_AUDIT_KEYS}
diff = {k: {"old": before[k], "new": after[k]} for k in STRATEGY_AUDIT_KEYS if before[k] != after[k]}
# diff 为空则仍记 STRATEGY_UPDATE，request_summary={"changed": {}}
```

### 4.3 Notifier 抽象

```python
class NotifierInterface(Protocol):
    def send(self, title: str, content: str, severity: str = "INFO") -> bool: ...

class ServerChanNotifier:
    def send(self, title: str, content: str, severity: str = "INFO") -> bool: ...
    def notify_order(...) -> bool: ...
    def notify_fill(...) -> bool: ...
    def notify_risk_event(self, event_type: str, reason: str, *, severity: str | None = None) -> bool:
        # severity 缺省时由 _severity_for_risk_event(event_type) 推导

class WebhookNotifier: ...

class MultiChannelNotifier:
    @classmethod
    def from_credential_config(cls, cred: CredentialConfig) -> "MultiChannelNotifier":
        # JSON 解析失败 → [(ServerChanNotifier(cred.sct_key), "INFO")] + log warning
        ...
```

**`notify_risk_event` severity 映射**（`notify.py` 单处 + 所有 call site 传显式 severity 或走默认表）：

| `event_type` | severity |
|--------------|----------|
| `REJECTED`, `ORDER_FAILED`, `ORDER_TIMEOUT`, `DAILY_LOSS`, 其他未列 | `WARNING` |
| `KILL_SWITCH` | `CRITICAL` |
| `ORDER_PERSISTENCE_FAILED` | `CRITICAL` |

**需改造的 call site：** `runner.py`（行情触发 risk 拒绝、`ORDER_FAILED`、reconcile 路径）、`trade_execution_service.py`（`_pause_after_failed_order`、`ORDER_TIMEOUT`、`ORDER_PERSISTENCE_FAILED`）。

### 4.4 交易时段守卫（双层，对齐 P4「撤单前 gate」）

**配置来源：** `StrategyService.get_config().trading_session_mode`；**市场：** `engine.params.market`（与 `is_trading_hours(market)` 一致）。

#### 层 A — `AppRunner`（LLM / 任何会 `cancel_pending` 的路径）

新增 `AppRunner._check_trading_session(action: str) -> dict | None`：

- `trading_session_mode != "RTH_ONLY"` → 返回 `None`（放行）
- `is_trading_hours(market)` → 返回 `None`
- 否则返回与 P4 `_precheck_llm_action` 相同形态的 skip dict（`executed: False`, `status: "SKIPPED"` 等），并：
  - `record_order_skipped` / `ORDER_SKIPPED`，`skip_category="SESSION"`
  - `_audit.record("TRADING_SESSION_BLOCKED", ...)`

**调用位置（均在 `cancel_pending_order` 之前）：**

- `execute_llm_order_decision`：`CANCEL_REPLACE` / 隐式替换 / 直接发单 —— 在 `_precheck_llm_action` **之后**、**任何** `cancel_pending_order` **之前**调用 `_check_trading_session`
- `CANCEL_PENDING` **仅撤单**：不拦（允许非 RTH 清理挂单）

#### 层 B — `TradeExecutionService.execute`（行情触发 + LLM 二次保险）

签名扩展：

```python
def execute(
    self,
    action: str,
    symbol: str,
    quote: Quote,
    broker: BrokerGateway,
    risk: RiskController,
    notifier: NotifierInterface,  # 原 ServerChanNotifier
    cash_currency: str,
    *,
    market: str,
    trading_session_mode: str = "ANY",
    ...
) -> OrderStatus | None:
```

在 **`risk.check()` 之前**：

```python
if trading_session_mode == "RTH_ONLY" and not is_trading_hours(market):
    return self._skip_order(symbol, action, f"non-RTH for {market}", skip_category="SESSION")
    # 若有 audit：TRADING_SESSION_BLOCKED（与层 A 重复写可接受，或层 B 仅 skip 不写 audit——实现择一，测试要求至少一处有 audit + ORDER_SKIPPED）
```

**`runner` 传参：** 行情触发与 `_execute_llm_trade_action` 均传入 `market=self.engine.params.market`、`trading_session_mode=config.trading_session_mode`（启动时缓存 config，strategy PUT 后 reload 或每轮读取——与现有 fee_rate 读取方式一致）。

- 前端 `skipCategoryLabel`：`SESSION → "非交易时段"`
- Dashboard 指示器文案注明：**不含节假日**，仅周末 + RTH 时段

### 4.5 Broker retry/backoff

```python
RETRYABLE_EXC = (...)  # T3 第一步 grep longport；否则降级字符串匹配（与 _is_auto_resumable_pause_reason 同源）

def _call_with_retry(
    self, fn, *, op: str, max_retries: int, base_ms: int,
) -> Any:
    # max_retries=0 → 只尝试 1 次，无 sleep（用于禁用重试）
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except RETRYABLE_EXC as exc:
            if attempt >= max_retries:
                raise
            delay = (base_ms / 1000.0) * (2 ** attempt)
            self._audit.record("BROKER_RETRY", ...)
            time.sleep(delay)
```

**包装范围（分档）：**

| 操作 | `AUTO_TRADE_BROKER_RETRY_MAX` | 说明 |
|------|-------------------------------|------|
| `submit_order`, `cancel_order` | 默认 `3` | 全量指数退避 |
| `get_quote`, `get_quotes` | 默认 `1`（可用 `AUTO_TRADE_BROKER_QUOTE_RETRY_MAX`） | 避免热路径 sleep 阻塞 runner 过久 |
| `get_candlesticks` | 同订单档或 `2` | LLM 分析非热路径 |

- 业务拒绝（余额不足、非法 tick 等）**不重试**
- retry 耗尽后上层仍走 `_is_auto_resumable_pause_reason` → pause

### 4.6 `AppRunner` 集成

- `self._audit = AuditLogger(SessionLocal)`
- `BrokerGateway(..., audit=self._audit)`（测试可 `audit=None` 跳过 `BROKER_RETRY` 写）
- `TradeExecutionService(..., audit=self._audit)`（可选）
- `self.notifier = MultiChannelNotifier.from_credential_config(cred)`；`_apply_credentials` 在凭证变更时重建
- `_ensure_*` 回填 `notification_channels` 时引用 **`CredentialConfig.sct_key`**

## 5. API 与配置

### 5.1 端点改造

- `GET /api/events`：`event_type` 支持**重复 query**（`?event_type=ORDER_SKIPPED&event_type=START`）或逗号分隔；新增 `source=trade|audit|all`（默认 `all`）
- `GET /api/events/export`：**不改**（仍仅 `trade_events`，见 YAGNI）
- 9 个写端点接入审计（§4.2）

### 5.2 跨表 union 与分页

列表项主键：**`(source, id)`**（`trade` 与 `audit` 的 `id` 可能碰撞，前端 `v-for` 必须用复合键）。

**`event_type` 筛选语义：**

- `source=trade`：`TradeEvent.event_type IN (...)`（如 `ORDER_SKIPPED`）
- `source=audit`：`AuditLog.action IN (...)`（如 `KILL_SWITCH`、`START`）；**无**把 `ORDER_SKIPPED` 映射到 audit 表
- `source=all`：两表分别按各自字段过滤后合并
- 响应统一：`TradeEventOut.event_type` = 原字段；`AuditEventOut.event_type` = **`action`**（便于 Timeline 单一列展示）

```python
def list_events(
    source: str,
    event_types: list[str] | None,
    page: int,
    page_size: int,
    db: Session,
) -> EventPageResponse:
    trade_filter = _trade_event_types_or_none(event_types)
    audit_filter = _audit_actions_or_none(event_types)

    if source in ("trade", "all"):
        trade_total = _count_trade_events(trade_filter)
    else:
        trade_total = 0
    if source in ("audit", "all"):
        audit_total = _count_audit_logs(audit_filter)
    else:
        audit_total = 0
    total = trade_total + audit_total

    # 过取：每表取 page * page_size 行，保证合并后排序切片不漏中间时间戳
    fetch_n = page * page_size
    trade_rows = [] if source == "audit" else _query_trade_events(trade_filter, limit=fetch_n)
    audit_rows = [] if source == "trade" else _query_audit_logs(audit_filter, limit=fetch_n)

    merged = sorted(
        [_to_trade_event_out(r) for r in trade_rows] + [_to_audit_event_out(r) for r in audit_rows],
        key=lambda e: (e.created_at, e.source, e.id),
        reverse=True,
    )
    start = (page - 1) * page_size
    items = merged[start : start + page_size]
    return EventPageResponse(items=items, total=total, page=page, page_size=page_size)
```

- SQLite 单标的量级下 `fetch_n = page * page_size` 可接受；数据量大时再改 `UNION ALL` SQL

### 5.3 新 schemas

- `EventOut` 基类：`id`, `event_type`, `created_at`, `payload`, **`source: Literal["trade","audit"]`**
- `AuditEventOut`：+ `actor_hash`, `source_ip`, `severity`, `result`（可选暴露）
- `TradeEventOut`：保持现有字段 + `source="trade"`
- `NotificationChannel` 嵌套 `CredentialResponse` / `CredentialConfigSchema`
- `StrategyConfigSchema` + `trading_session_mode`

### 5.4 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `AUTO_TRADE_BROKER_RETRY_MAX` | `3` | 订单类最大**重试次数**（0 = 不重试，共 1 次调用） |
| `AUTO_TRADE_BROKER_QUOTE_RETRY_MAX` | `1` | 行情类重试次数 |
| `AUTO_TRADE_BROKER_RETRY_BASE_MS` | `1000` | 指数退避基数 |
| `AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT` | `2048` | request_summary 截断 |

## 6. 前端

### 6.1 Strategy（`Strategy.vue`）

- 「交易时段」：`RTH_ONLY` / `ANY`；帮助文本说明**不含节假日**
- `PUT /api/strategy` 含 `trading_session_mode`

### 6.2 Credentials（`Credentials.vue`）

- 通知渠道列表；类型 `serverchan` | `webhook`
- 保留现有 **`sct_key`** 输入；保存时写入 `notification_channels` + `sct_key`
- 至少一条渠道；Webhook 须 http(s)

### 6.3 Decision Timeline

- 多选 `event_type` + `source` 切换
- 审计卡片：severity 色条、`action`、`actor_hash` 前 8 位、`source_ip`、`request_summary` 折叠
- 列表 `:key="`${item.source}-${item.id}`"`"

### 6.4 Dashboard

- `SESSION → 非交易时段`
- 指示器：`trading_session_mode` + `is_trading_hours`；副文案「不含节假日」

### 6.5 API / Types

- `events.ts`：`source`、多选 `event_type`
- `strategy.ts` / `credentials.ts` 新字段
- `SkipCategory` + `'SESSION'`；`AuditEvent`、`NotificationChannel` 等

## 7. 测试与验证

### 7.1 Backend pytest（~30 新增）

| 文件 | 覆盖 |
|------|------|
| `test_audit_logger.py` | 写入、截断、hash、IP、失败 fallback |
| `test_notifier_multi_channel.py` | severity_floor、fan-out、risk severity |
| `test_webhook_notifier.py` | payload、超时、非 2xx |
| `test_broker_retry.py` | 订单重试/退避；`max_retries=0` 只调 1 次；行情档更短 |
| `test_trading_session_guard.py` | `RTH_ONLY` + SESSION skip；**LLM CANCEL_REPLACE 非 RTH 不撤 pending**；`CANCEL_PENDING` 仍允许；`ANY` 放行 |
| `test_api.py` | 9 端点审计 SUCCESS/FAILED；`GET /api/events` 分页 total、`(source,id)` |
| `test_credentials_api.py` | `notification_channels`、脱敏 audit |
| `test_runner.py` | notifier 重建、多通道 CRITICAL |
| `test_database.py` | 三个 `_ensure_*` 幂等 |
| `test_trade_execution_service.py` | execute 层 SESSION skip |

### 7.2 Cypress（3 spec）

不变：`credentials_notifications`、`decision_timeline_audit`、`strategy_session_guard`（含「不含节假日」文案可选断言）。

### 7.3 Lint

- `basedpyright` 0 / 0；`npm run type-check` + `build` 通过

### 7.4 手工验证

- [ ] 旧 DB：`audit_logs` + `notification_channels` 回填
- [ ] Webhook + Kill Switch → 多渠道 CRITICAL
- [ ] `RTH_ONLY` 非 RTH：无新单；LLM 替换**不**先撤单；`CANCEL_PENDING` 可撤
- [ ] broker 限流 → `BROKER_RETRY` 行；`BROKER_RETRY_MAX=0` 无 sleep 重试
- [ ] 渠道删光 → 前端阻止保存
- [ ] Timeline 第 2 页与 `total` 与第 1 页无重复/遗漏（抽测）

## 8. 风险与回滚

| 风险 | 缓解 |
|------|------|
| longport 无结构化异常 | 字符串降级 + retry 循环 |
| union 过取 `page*page_size` 变慢 | 单标的 SQLite；后续 SQL UNION |
| `notification_channels` 坏 JSON | 回退 `sct_key` 单通道 + warning |
| `RTH_ONLY` 不含假日 | UI 明示；默认 `ANY` |
| 审计 actor 多为 anonymous | 记 IP；P2 排除已文档化 |
| retry sleep 阻塞 runner | 行情低 `QUOTE_RETRY_MAX` |

**回滚：**

- `trading_session_mode=ANY`
- `AUTO_TRADE_BROKER_RETRY_MAX=0` 且 `AUTO_TRADE_BROKER_QUOTE_RETRY_MAX=0`（均无退避重试）
- 三个 `_ensure_*` 可独立 revert

## 9. 交付顺序

1. **T1** — AuditLog
2. **T4** — Notifier（可与 T1 并行）
3. **T2** — 时段守卫（层 A + 层 B）
4. **T3** — Broker retry（先 grep 异常）
5. **T5** — 前端
6. **T6** — 测试与 lint 全绿

## 10. 不在本迭代

- P6–P8、API 鉴权收紧（P2）
- 审计独立页、Webhook 模板、通知重发队列、**审计 CSV/JSON 导出**
