# P25: 运行时策略参数热重载 — `margin_safety_factor` 链路修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `margin_safety_factor` 从配置保存到交易执行的全链路断裂，使其支持运行时热重载。

**Architecture:** 在现有 `reload_strategy()` 机制上补充 `margin_safety_factor` 的传递链路：API 层接收 → 服务层持久化 → Runner 层注入 → 执行层消费。执行层优先使用实例属性，回退到硬编码常量。

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest

---

## 文件结构

| 文件 | 职责 | 改动类型 |
|------|------|---------|
| `backend/app/services/trade_execution_service.py` | `TradeExecutionService` 增加 `margin_safety_factor` 实例属性；`_entry_quantity_from_margin_power` 优先使用实例属性 | 修改 |
| `backend/app/runner.py` | `AppRunner.__init__` 初始化时传入 `margin_safety_factor`；`reload_strategy()` 更新 `_trade_svc.margin_safety_factor` | 修改 |
| `backend/app/services/strategy_service.py` | `STRATEGY_AUDIT_KEYS` 和 `updatable_fields` 追加 `margin_safety_factor` | 修改 |
| `backend/app/api/strategy.py` | `PUT /api/strategy` 的 merged 字典追加 `margin_safety_factor` | 修改 |
| `backend/tests/test_trade_execution_service.py` | 补充 `margin_safety_factor` 实例属性及 `_entry_quantity_from_margin_power` 消费测试 | 修改 |
| `backend/tests/test_runner.py` | 补充 `reload_strategy()` 后 `_trade_svc.margin_safety_factor` 被更新的断言 | 修改 |
| `backend/tests/test_strategy_service.py` | 补充 `update_config` 对 `margin_safety_factor` 的持久化验证 | 修改 |
| `backend/tests/test_api.py` | 补充 `PUT /api/strategy` 对 `margin_safety_factor` 的传递与响应验证 | 修改 |

---

## Task 1: TradeExecutionService 增加 `margin_safety_factor` 属性

**Files:**
- Modify: `backend/app/services/trade_execution_service.py:102-123`
- Modify: `backend/app/services/trade_execution_service.py:264-286`
- Test: `backend/tests/test_trade_execution_service.py`

- [ ] **Step 1: 给 `__init__` 增加 `margin_safety_factor` 参数并保存为实例属性**

修改 `backend/app/services/trade_execution_service.py` 的 `__init__` 方法：

```python
class TradeExecutionService:
    def __init__(
        self,
        record_order: Callable[[str, str, str, float, float, str], None],
        update_order_status: Callable[[str, str, datetime | None, float | None, float | None], None],
        record_risk_event: Callable[[str], None],
        record_order_skipped: _RecordOrderSkipped | None = None,
        persist_entry: _EntryPersistCallback | None = None,
        audit: AuditLogger | None = None,
        margin_safety_factor: float | None = None,
    ) -> None:
        self._record_order = record_order
        self._update_order_status = update_order_status
        self._record_risk_event = record_risk_event
        self._record_order_skipped = record_order_skipped
        self._persist_entry = persist_entry
        self._audit = audit
        self.margin_safety_factor = margin_safety_factor
        self._state_lock = RLock()
        self._pending_order: _PendingOrder | None = None
        self._order_status_poll_interval_seconds = 1.0
        self._order_status_timeout_seconds = 30.0
        self._entry_positions: dict[str, _TrackedEntry] = {}
```

- [ ] **Step 2: 修改 `_entry_quantity_from_margin_power` 优先使用实例属性**

修改 `backend/app/services/trade_execution_service.py` 的 `_entry_quantity_from_margin_power` 方法：

```python
    def _entry_quantity_from_margin_power(
        self,
        broker: BrokerGateway,
        symbol: str,
        side: str,
        price: Decimal,
        cash_currency: str,
        *,
        safety_factor: float | None = None,
    ) -> int:
        max_qty = broker.estimate_margin_max_quantity(symbol, side, price, cash_currency)
        if safety_factor is not None:
            factor = Decimal(str(safety_factor))
        elif self.margin_safety_factor is not None:
            factor = Decimal(str(self.margin_safety_factor))
        else:
            factor = ENTRY_BUYING_POWER_USAGE
        qty = int(max_qty * factor)
        if qty <= 0:
            logger.warning(
                "%s: qty <= 0, margin_max_qty=%s price=%s currency=%s factor=%s",
                side,
                max_qty,
                price,
                cash_currency,
                factor,
            )
        return qty
```

- [ ] **Step 3: 编写测试验证实例属性及消费逻辑**

在 `backend/tests/test_trade_execution_service.py` 中增加测试：

```python
    def test_margin_safety_factor_instance_attribute(self, svc: TradeExecutionService) -> None:
        assert svc.margin_safety_factor is None
        svc.margin_safety_factor = 0.75
        assert svc.margin_safety_factor == 0.75

    def test_entry_quantity_uses_instance_margin_safety_factor(self, svc: TradeExecutionService) -> None:
        from unittest.mock import MagicMock
        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        svc.margin_safety_factor = 0.75
        qty = svc._entry_quantity_from_margin_power(broker, "AAPL.US", "BUY", Decimal("150"), "USD")
        assert qty == 75  # 100 * 0.75

    def test_entry_quantity_keyword_overrides_instance_attribute(self, svc: TradeExecutionService) -> None:
        from unittest.mock import MagicMock
        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        svc.margin_safety_factor = 0.75
        qty = svc._entry_quantity_from_margin_power(broker, "AAPL.US", "BUY", Decimal("150"), "USD", safety_factor=0.5)
        assert qty == 50  # 100 * 0.5

    def test_entry_quantity_fallback_to_constant(self, svc: TradeExecutionService) -> None:
        from unittest.mock import MagicMock
        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        svc.margin_safety_factor = None
        qty = svc._entry_quantity_from_margin_power(broker, "AAPL.US", "BUY", Decimal("150"), "USD")
        assert qty == 90  # 100 * 0.9 (ENTRY_BUYING_POWER_USAGE)
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python3 -m pytest tests/test_trade_execution_service.py::TestTradeExecutionService::test_margin_safety_factor_instance_attribute tests/test_trade_execution_service.py::TestTradeExecutionService::test_entry_quantity_uses_instance_margin_safety_factor tests/test_trade_execution_service.py::TestTradeExecutionService::test_entry_quantity_keyword_overrides_instance_attribute tests/test_trade_execution_service.py::TestTradeExecutionService::test_entry_quantity_fallback_to_constant -v`

Expected: 4 PASSED

---

## Task 2: AppRunner 初始化和 reload 注入 `margin_safety_factor`

**Files:**
- Modify: `backend/app/runner.py:67-74`
- Modify: `backend/app/runner.py:274-317`
- Test: `backend/tests/test_runner.py`

- [ ] **Step 1: `AppRunner.__init__` 初始化 `_trade_svc` 时传入 `margin_safety_factor`**

修改 `backend/app/runner.py` 的 `__init__`：

```python
        self._trade_svc = TradeExecutionService(
            record_order=self._record_order,
            update_order_status=self._update_order_status,
            record_risk_event=self._record_risk_event,
            record_order_skipped=self._record_order_skipped,
            persist_entry=self._persist_tracked_entry,
            audit=self._audit,
            margin_safety_factor=settings.margin_safety_factor,
        )
```

**注意：** 需要确认 `settings.margin_safety_factor` 是否存在。如果不存在，从 `config.py` 中检查或直接使用 `None`（回退到常量）。

如果 `settings` 中没有 `margin_safety_factor`，使用 `None`：

```python
            margin_safety_factor=None,
```

- [ ] **Step 2: `reload_strategy()` 中更新 `_trade_svc.margin_safety_factor`**

在 `backend/app/runner.py` 的 `reload_strategy()` 方法中，在更新 `_trading_session_mode` 之后增加：

```python
                self._trade_svc.margin_safety_factor = getattr(config, "margin_safety_factor", None)
```

完整上下文（插入到第 299 行之后）：

```python
                mode = getattr(config, "trading_session_mode", None)
                self._trading_session_mode = mode if mode else "ANY"
                self._trade_svc.margin_safety_factor = getattr(config, "margin_safety_factor", None)
```

- [ ] **Step 3: 编写测试验证 reload 后属性更新**

在 `backend/tests/test_runner.py` 中增加测试：

```python
    def test_reload_strategy_updates_margin_safety_factor(self, monkeypatch) -> None:
        from app.services.strategy_service import StrategyService
        from app.models import StrategyConfig

        runner = AppRunner()
        runner._trade_svc.margin_safety_factor = None

        class FakeConfig:
            symbol = "AAPL.US"
            market = "US"
            buy_low = 100.0
            sell_high = 200.0
            short_selling = False
            min_profit_amount = 0.0
            auto_resume_minutes = 3
            fee_rate_us = 0.0005
            fee_rate_hk = 0.003
            min_repricing_pct = 0.003
            llm_action_cooldown_seconds = 60
            trading_session_mode = "ANY"
            margin_safety_factor = 0.75

        class FakeSvc:
            def get_config(self):
                return FakeConfig()

        monkeypatch.setattr(StrategyService, "__init__", lambda self, db: None)
        monkeypatch.setattr(StrategyService, "get_config", FakeSvc().get_config)
        monkeypatch.setattr(runner.broker, "unsubscribe_quotes", lambda: None)
        monkeypatch.setattr(runner.broker, "subscribe_quotes", lambda symbol, callback: None)

        runner.reload_strategy()
        assert runner._trade_svc.margin_safety_factor == 0.75
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python3 -m pytest tests/test_runner.py::TestAppRunner::test_reload_strategy_updates_margin_safety_factor -v`

Expected: 1 PASSED

---

## Task 3: StrategyService 持久化 `margin_safety_factor`

**Files:**
- Modify: `backend/app/services/strategy_service.py:10-26`
- Modify: `backend/app/services/strategy_service.py:49-56`
- Test: `backend/tests/test_strategy_service.py`

- [ ] **Step 1: `STRATEGY_AUDIT_KEYS` 追加 `margin_safety_factor`**

修改 `backend/app/services/strategy_service.py`：

```python
STRATEGY_AUDIT_KEYS = (
    "symbol",
    "market",
    "buy_low",
    "sell_high",
    "short_selling",
    "min_profit_amount",
    "auto_resume_minutes",
    "max_daily_loss",
    "max_consecutive_losses",
    "llm_interval_minutes",
    "fee_rate_us",
    "fee_rate_hk",
    "min_repricing_pct",
    "llm_action_cooldown_seconds",
    "trading_session_mode",
    "margin_safety_factor",
)
```

- [ ] **Step 2: `updatable_fields` 追加 `margin_safety_factor`**

修改 `backend/app/services/strategy_service.py`：

```python
        updatable_fields = [
            "symbol", "market", "buy_low", "sell_high",
            "short_selling", "min_profit_amount", "auto_resume_minutes",
            "max_daily_loss", "max_consecutive_losses",
            "llm_interval_minutes",
            "fee_rate_us", "fee_rate_hk", "min_repricing_pct", "llm_action_cooldown_seconds",
            "trading_session_mode",
            "margin_safety_factor",
        ]
```

- [ ] **Step 3: 编写测试验证持久化**

在 `backend/tests/test_strategy_service.py` 的 `test_update_config` 中增加断言：

```python
    def test_update_config_persists_margin_safety_factor(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        updated, diff = svc.update_config({
            "symbol": "AAPL.US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "margin_safety_factor": 0.75,
        })
        assert updated.margin_safety_factor == 0.75
        assert "margin_safety_factor" in diff
        assert diff["margin_safety_factor"]["new"] == 0.75

        config = svc.get_config()
        assert config.margin_safety_factor == 0.75
        db.close()
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python3 -m pytest tests/test_strategy_service.py::TestStrategyService::test_update_config_persists_margin_safety_factor -v`

Expected: 1 PASSED

---

## Task 4: API 层接收并传递 `margin_safety_factor`

**Files:**
- Modify: `backend/app/api/strategy.py:64-80`
- Test: `backend/tests/test_api.py`

- [ ] **Step 1: merged 字典追加 `margin_safety_factor`**

在 `backend/app/api/strategy.py` 的 `put_strategy` 中，merged 字典增加：

```python
        merged = {
            ...,
            "margin_safety_factor": data["margin_safety_factor"] if "margin_safety_factor" in data and data["margin_safety_factor"] is not None else current.margin_safety_factor,
        }
```

- [ ] **Step 2: 编写测试验证 API 传递**

在 `backend/tests/test_api.py` 中增加测试（放在现有的 strategy update 测试附近）：

```python
    def test_update_strategy_persists_margin_safety_factor(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "margin_safety_factor": 0.75,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["margin_safety_factor"] == 0.75

        # verify read-back
        resp2 = client.get("/api/strategy")
        assert resp2.status_code == 200
        assert resp2.json()["margin_safety_factor"] == 0.75
```

- [ ] **Step 3: 运行测试**

Run: `cd backend && python3 -m pytest tests/test_api.py::TestApi::test_update_strategy_persists_margin_safety_factor -v`

Expected: 1 PASSED

---

## Task 5: 全量验证

- [ ] **Step 1: 运行后端全部测试**

Run: `cd backend && python3 -m pytest tests/ -v`

Expected: 全部通过（基线 750 passed + 新增 ≥6 项）

- [ ] **Step 2: 类型检查**

Run: `cd backend && python3 -m basedpyright`

Expected: 0 errors / 0 warnings / 0 notes

- [ ] **Step 3: 前端构建**

Run: `cd frontend && npm run type-check && npm run build`

Expected: 0 errors, build 成功

---

## Self-Review

### Spec Coverage
- [x] API 层接收 `margin_safety_factor` — Task 4
- [x] 服务层持久化 `margin_safety_factor` — Task 3
- [x] Runner 层 reload 注入 — Task 2
- [x] 执行层消费实例属性 — Task 1
- [x] 回退到常量 — Task 1 Step 3 test 4
- [x] 关键字参数覆盖实例属性 — Task 1 Step 3 test 3

### Placeholder Scan
- [x] 无 TBD/TODO/"implement later"
- [x] 所有步骤含完整代码
- [x] 所有步骤含精确命令

### Type Consistency
- [x] `margin_safety_factor` 类型统一为 `float | None`
- [x] `Decimal(str(...))` 转换一致
