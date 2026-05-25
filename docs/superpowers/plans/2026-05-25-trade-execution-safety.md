# 交易执行安全与成本控制实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 P4：实盘普通平仓按费用后净收益保护，LLM 撤单重挂在撤单前完成改价与冷却校验，并在前端显示稳定的订单跳过原因。

**Architecture:** 四项安全配置沿现有 `StrategyConfig -> StrategyService/API -> StrategyParams -> AppRunner/TradeExecutionService` 路径流转。纯函数费用计算由 `app/core/fees.py` 负责，普通平仓费用保护仍留在已有执行服务；LLM 特有的替换与节流 gate 留在持有 pending 上下文的 `AppRunner`，且必须发生在撤单前。回测保留独立试算参数，仅补跳过原因分类。

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, SQLite runtime migrations, pytest, Vue 3, TypeScript, Element Plus, Cypress.

---

## 文件结构

### 后端新增文件

| 文件 | 职责 |
|---|---|
| `backend/app/core/fees.py` | 使用 `Decimal` 估算实盘 round-trip 费用并按市场选用配置费率 |
| `backend/tests/test_fees.py` | 费用纯函数测试 |

### 后端修改文件

| 文件 | 改动 |
|---|---|
| `backend/app/models.py` | `StrategyConfig` 增加四项 P4 配置字段 |
| `backend/app/schemas.py` | 策略请求/响应字段校验；回测 skip 分类响应 |
| `backend/app/database.py` | SQLite 运行时补列 `_ensure_strategy_config_trade_safety_columns()` |
| `backend/app/api/strategy.py` | 合并并返回四项策略安全设置 |
| `backend/app/services/strategy_service.py` | 允许更新四项配置 |
| `backend/app/core/engine.py` | `StrategyParams` 承载四项运行时配置 |
| `backend/app/services/runtime_state_service.py` | 启动加载配置到 `StrategyParams` |
| `backend/app/runner.py` | reload 时加载配置；向执行服务传费率；在撤单前执行 LLM gate 并记录分类 |
| `backend/app/services/trade_execution_service.py` | 费用后退出门槛；统一 risk/pending/position/fee skip 分类；暴露 pending 只读视图 |
| `backend/app/core/backtest.py` | `BacktestSkippedSignal.category` |
| `backend/app/api/backtest.py` | 返回回测 skip 分类 |

### 前端修改文件

| 文件 | 改动 |
|---|---|
| `frontend/src/types/index.ts` | 策略设置与回测 skip 类型字段 |
| `frontend/src/composables/useDashboardData.ts` | 策略默认值补四项配置 |
| `frontend/src/views/Strategy.vue` | 成本与执行保护设置表单 |
| `frontend/src/utils/labels.ts` | `skipCategoryLabel()` |
| `frontend/src/views/DecisionTimeline.vue` | 跳过分类列与当前页筛选 |
| `frontend/src/views/Dashboard.vue` | 最近跳过事件展示分类 |
| `frontend/src/views/Backtest.vue` | 回测跳过信号展示分类 |
| `frontend/cypress/support/e2e.ts` | API stub 加新增字段与分类 payload |

### 测试与文档修改文件

| 文件 | 覆盖内容 |
|---|---|
| `backend/tests/test_models.py`、`backend/tests/test_database.py`、`backend/tests/test_api.py` | 配置默认值、补列、API 保存与校验 |
| `backend/tests/test_runtime_state_service.py`、`backend/tests/test_runner.py` | 运行时配置注入、LLM gate |
| `backend/tests/test_trade_execution_service.py`、`backend/tests/test_trade_event_sync.py` | 费用保护与 skip 分类 |
| `backend/tests/test_backtest.py` | 回测费用 skip 分类 |
| `frontend/cypress/e2e/strategy.cy.ts`、`events.cy.ts`、`dashboard.cy.ts`、`backtest.cy.ts` | 页面展示与编辑 |
| `README.md`、`CLAUDE.md`、`docs/Roadmap.md` | 已交付行为与 P4 完成状态 |

---

### Task 1: 配置持久化与运行时传递

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/database.py`
- Modify: `backend/app/services/strategy_service.py`
- Modify: `backend/app/core/engine.py`
- Modify: `backend/app/services/runtime_state_service.py`
- Modify: `backend/app/runner.py`
- Test: `backend/tests/test_models.py`
- Test: `backend/tests/test_database.py`
- Test: `backend/tests/test_api.py`
- Test: `backend/tests/test_runtime_state_service.py`

- [ ] **Step 1: 为默认值、API round-trip 和旧 SQLite 补列编写失败测试**

在 `backend/tests/test_models.py::test_strategy_config_persistence` 增加默认值断言：

```python
            assert result.fee_rate_us == 0.0005
            assert result.fee_rate_hk == 0.003
            assert result.min_repricing_pct == 0.003
            assert result.llm_action_cooldown_seconds == 60
```

在 `backend/tests/test_api.py` 的模块初始化中调用新的 ensure 函数，并加入 API 用例：

```python
database._ensure_strategy_config_trade_safety_columns(db_engine)

    def test_update_strategy_allows_trade_safety_settings(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "fee_rate_us": 0.001,
            "fee_rate_hk": 0.004,
            "min_repricing_pct": 0.004,
            "llm_action_cooldown_seconds": 120,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["fee_rate_us"] == 0.001
        assert data["fee_rate_hk"] == 0.004
        assert data["min_repricing_pct"] == 0.004
        assert data["llm_action_cooldown_seconds"] == 120

    def test_update_strategy_rejects_invalid_trade_safety_settings(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "fee_rate_us": -0.01,
            "min_repricing_pct": 0.06,
            "llm_action_cooldown_seconds": 3601,
        })

        assert resp.status_code == 422
```

在 `backend/tests/test_database.py` 增加旧表迁移用例：

```python
def test_init_db_adds_missing_strategy_trade_safety_columns(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy_strategy_trade_safety.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE strategy_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol VARCHAR(50) NOT NULL,
                market VARCHAR(10) NOT NULL,
                buy_low FLOAT NOT NULL,
                sell_high FLOAT NOT NULL,
                short_selling BOOLEAN NOT NULL,
                max_daily_loss FLOAT NOT NULL,
                max_consecutive_losses INTEGER NOT NULL,
                sct_key VARCHAR(200) NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()

    with engine.connect() as db:
        columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(strategy_config)")}
    assert {"fee_rate_us", "fee_rate_hk", "min_repricing_pct", "llm_action_cooldown_seconds"} <= columns
```

在 `backend/tests/test_runtime_state_service.py` 的配置加载断言中补充：

```python
    assert engine.params.fee_rate_us == 0.001
    assert engine.params.fee_rate_hk == 0.004
    assert engine.params.min_repricing_pct == 0.004
    assert engine.params.llm_action_cooldown_seconds == 120
```

- [ ] **Step 2: 运行配置测试，确认新增断言失败**

Run:

```bash
cd backend && python3 -m pytest tests/test_models.py tests/test_database.py tests/test_api.py tests/test_runtime_state_service.py -v
```

Expected: FAIL，提示 `StrategyConfig` / 响应模型 / `StrategyParams` 尚无新增字段，或数据库 ensure 函数不存在。

- [ ] **Step 3: 增加 ORM、schema、迁移和策略服务字段**

在 `backend/app/models.py::StrategyConfig` 的现有风险参数后加入：

```python
    fee_rate_us: Mapped[float] = mapped_column(Float, default=0.0005)
    fee_rate_hk: Mapped[float] = mapped_column(Float, default=0.0030)
    min_repricing_pct: Mapped[float] = mapped_column(Float, default=0.003)
    llm_action_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60)
```

在 `StrategyConfigSchema` 加入：

```python
    fee_rate_us: Optional[float] = Field(default=None, ge=0, le=0.01)
    fee_rate_hk: Optional[float] = Field(default=None, ge=0, le=0.02)
    min_repricing_pct: Optional[float] = Field(default=None, ge=0, le=0.05)
    llm_action_cooldown_seconds: Optional[int] = Field(default=None, ge=0, le=3600)
```

在 `StrategyMergedSchema` 使用非可空落库默认：

```python
    fee_rate_us: float = Field(default=0.0005, ge=0, le=0.01)
    fee_rate_hk: float = Field(default=0.0030, ge=0, le=0.02)
    min_repricing_pct: float = Field(default=0.003, ge=0, le=0.05)
    llm_action_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
```

在 `StrategyResponse` 加入非可空响应字段：

```python
    fee_rate_us: float
    fee_rate_hk: float
    min_repricing_pct: float
    llm_action_cooldown_seconds: int
```

在 `backend/app/services/strategy_service.py::update_config` 的 `updatable_fields` 增加：

```python
            "fee_rate_us", "fee_rate_hk", "min_repricing_pct",
            "llm_action_cooldown_seconds",
```

在 `backend/app/api/strategy.py::put_strategy` 的 `merged` dict 加入：

```python
        "fee_rate_us": data["fee_rate_us"] if data.get("fee_rate_us") is not None else current.fee_rate_us,
        "fee_rate_hk": data["fee_rate_hk"] if data.get("fee_rate_hk") is not None else current.fee_rate_hk,
        "min_repricing_pct": data["min_repricing_pct"] if data.get("min_repricing_pct") is not None else current.min_repricing_pct,
        "llm_action_cooldown_seconds": data["llm_action_cooldown_seconds"] if data.get("llm_action_cooldown_seconds") is not None else current.llm_action_cooldown_seconds,
```

- [ ] **Step 4: 增加 SQLite 运行时补列**

在 `backend/app/database.py::init_db()` 调用：

```python
    _ensure_strategy_config_trade_safety_columns(engine)
```

在同文件新增：

```python
def _ensure_strategy_config_trade_safety_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "strategy_config" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("strategy_config")}
    missing_columns = {
        "fee_rate_us": "FLOAT DEFAULT 0.0005 NOT NULL",
        "fee_rate_hk": "FLOAT DEFAULT 0.003 NOT NULL",
        "min_repricing_pct": "FLOAT DEFAULT 0.003 NOT NULL",
        "llm_action_cooldown_seconds": "INTEGER DEFAULT 60 NOT NULL",
    }.items()
    with db_engine.begin() as connection:
        for name, column_type in missing_columns:
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE strategy_config ADD COLUMN {name} {column_type}"
                )
```

- [ ] **Step 5: 将配置装入运行时 `StrategyParams`**

在 `backend/app/core/engine.py::StrategyParams` 添加：

```python
    fee_rate_us: float = 0.0005
    fee_rate_hk: float = 0.0030
    min_repricing_pct: float = 0.003
    llm_action_cooldown_seconds: int = 60
```

在 `backend/app/services/runtime_state_service.py::load()` 与 `backend/app/runner.py::reload_strategy()` 的 `StrategyParams` 构造器内分别加入：

```python
            fee_rate_us=config.fee_rate_us,
            fee_rate_hk=config.fee_rate_hk,
            min_repricing_pct=config.min_repricing_pct,
            llm_action_cooldown_seconds=config.llm_action_cooldown_seconds,
```

- [ ] **Step 6: 运行配置测试，确认通过**

Run:

```bash
cd backend && python3 -m pytest tests/test_models.py tests/test_database.py tests/test_api.py tests/test_runtime_state_service.py -v
```

Expected: PASS。

- [ ] **Step 7: 提交配置链路**

```bash
git add backend/app/models.py backend/app/schemas.py backend/app/database.py backend/app/services/strategy_service.py backend/app/api/strategy.py backend/app/core/engine.py backend/app/services/runtime_state_service.py backend/app/runner.py backend/tests/test_models.py backend/tests/test_database.py backend/tests/test_api.py backend/tests/test_runtime_state_service.py
git commit -m "feat: add trade safety strategy settings"
```

---

### Task 2: 实盘费用估算与退出保护

**Files:**
- Create: `backend/app/core/fees.py`
- Create: `backend/tests/test_fees.py`
- Modify: `backend/app/services/trade_execution_service.py`
- Modify: `backend/app/runner.py`
- Test: `backend/tests/test_trade_execution_service.py`
- Test: `backend/tests/test_trade_event_sync.py`

- [ ] **Step 1: 编写费用纯函数失败测试**

创建 `backend/tests/test_fees.py`：

```python
from decimal import Decimal

from app.core.fees import estimate_round_trip_fee, one_side_fee_rate


def test_estimate_round_trip_fee_uses_entry_and_exit_notional() -> None:
    assert estimate_round_trip_fee(
        entry_price=Decimal("100"),
        exit_price=Decimal("102"),
        quantity=Decimal("10"),
        one_side_rate=Decimal("0.001"),
    ) == Decimal("2.020")


def test_one_side_fee_rate_selects_market_setting() -> None:
    assert one_side_fee_rate("US", Decimal("0.0005"), Decimal("0.003")) == Decimal("0.0005")
    assert one_side_fee_rate("HK", Decimal("0.0005"), Decimal("0.003")) == Decimal("0.003")
```

- [ ] **Step 2: 编写费用后退出与 skip 分类失败测试**

在 `backend/tests/test_trade_execution_service.py` 增加一个记录事件的 fixture 或在测试内实例化 service：

```python
    def test_execute_sell_skips_when_fees_reduce_net_profit_below_minimum(self, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import Position, Quote
        from app.core.notify import ServerChanNotifier
        from app.core.risk import RiskController

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.0)
        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda *args: skipped.append(args),
        )
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("10"), Decimal("100"))]

        status = svc.execute(
            "SELL", "NVDA.US", Quote("NVDA.US", 101, 100.9, 101.1, ""),
            broker, RiskController(), ServerChanNotifier(""), "USD",
            min_profit_amount=Decimal("9"), fee_rate=Decimal("0.001"),
        )

        assert status is not None and status.status == "SKIPPED"
        assert skipped[0][3]["skip_category"] == "FEE"
        assert skipped[0][3]["estimated_fees"] == 2.01
        broker.submit_limit_order.assert_not_called()

    def test_execute_sell_stop_loss_does_not_apply_fee_gate(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.notify import ServerChanNotifier
        from app.core.risk import RiskController

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.2)
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("10"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult(
            "stop-loss-fee", "NVDA.US", "SELL", Decimal("10"), Decimal("215"), "FILLED"
        )

        status = svc.execute(
            "SELL", "NVDA.US", Quote("NVDA.US", 215, 214.9, 215.1, ""),
            broker, RiskController(), ServerChanNotifier(""), "USD",
            min_profit_amount=Decimal("50"), fee_rate=Decimal("0.5"), allow_loss_exit=True,
        )

        assert status is not None and status.status == "FILLED"
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("10"), Decimal("215"))
```

新增 risk、pending 与无可用持仓分类测试，均使用带记录 callback 的 service：

```python
    def test_execute_risk_rejection_records_risk_skip_category(self) -> None:
        from app.core.broker import Quote
        from app.core.notify import ServerChanNotifier
        from app.core.risk import RiskController

        skipped: list[tuple] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda *args: skipped.append(args),
        )
        risk = RiskController()
        risk.pause("paused by test")
        status = svc.execute(
            "BUY", "NVDA.US", Quote("NVDA.US", 100, 99, 101, ""),
            MagicMock(), risk, ServerChanNotifier(""), "USD",
        )
        assert status is not None and status.status == "SKIPPED"
        assert skipped[0][3]["skip_category"] == "RISK"

    def test_execute_pending_rejection_records_pending_skip_category(self) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.notify import ServerChanNotifier
        from app.core.risk import RiskController

        skipped: list[tuple] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda *args: skipped.append(args),
        )
        svc._track_pending_order(
            "BUY",
            OrderResult("pending-1", "NVDA.US", "BUY", Decimal("1"), Decimal("100"), "SUBMITTED"),
            MagicMock(),
            None,
        )
        status = svc.execute(
            "BUY", "NVDA.US", Quote("NVDA.US", 100, 99, 101, ""),
            MagicMock(), RiskController(), ServerChanNotifier(""), "USD",
        )
        assert status is not None and status.status == "SKIPPED"
        assert skipped[0][3]["skip_category"] == "PENDING"

    def test_execute_sell_without_available_quantity_records_position_category(self) -> None:
        from app.core.broker import Position, Quote
        from app.core.notify import ServerChanNotifier
        from app.core.risk import RiskController

        skipped: list[tuple] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda *args: skipped.append(args),
        )
        broker = MagicMock()
        broker.get_positions.return_value = [
            Position("NVDA.US", "LONG", Decimal("10"), Decimal("100"), available_quantity=Decimal("0"))
        ]
        status = svc.execute(
            "SELL", "NVDA.US", Quote("NVDA.US", 102, 101, 103, ""),
            broker, RiskController(), ServerChanNotifier(""), "USD",
        )
        assert status is not None and status.status == "SKIPPED"
        assert skipped[0][3]["skip_category"] == "POSITION"
```

在 `backend/tests/test_trade_event_sync.py::test_record_order_skipped_writes_trade_event` 将 payload 改为：

```python
            {"skip_category": "FEE", "expected_profit": 4.0, "required_profit": 5.0},
```

并断言：

```python
            assert '"skip_category": "FEE"' in event.payload_json
```

- [ ] **Step 3: 运行费用/执行测试，确认失败**

Run:

```bash
cd backend && python3 -m pytest tests/test_fees.py tests/test_trade_execution_service.py tests/test_trade_event_sync.py -v
```

Expected: FAIL，原因包括 `app.core.fees` 不存在、`execute()` 不接受 `fee_rate`、现有 risk/pending 路径未记录分类。

- [ ] **Step 4: 实现纯费用模块**

创建 `backend/app/core/fees.py`：

```python
from __future__ import annotations

from decimal import Decimal


def one_side_fee_rate(market: str, fee_rate_us: Decimal, fee_rate_hk: Decimal) -> Decimal:
    return fee_rate_hk if market.upper() == "HK" else fee_rate_us


def estimate_round_trip_fee(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    one_side_rate: Decimal,
) -> Decimal:
    if quantity <= 0 or one_side_rate <= 0:
        return Decimal("0")
    return (entry_price + exit_price) * quantity * one_side_rate
```

- [ ] **Step 5: 修改执行服务的费用 gate 与分类写入**

在 `backend/app/services/trade_execution_service.py` 导入 `estimate_round_trip_fee`；扩展 `execute()`、`_execute_sell()` 与 `_execute_buy_to_cover()` 参数：

```python
        fee_rate: Decimal | float | int = Decimal("0"),
```

将 `_profit_guard_for_exit()` 扩展为：

```python
        fee_rate: Decimal | float | int,
```

并在毛利计算后添加：

```python
        rate = self._coerce_non_negative_decimal(fee_rate)
        estimated_fees = estimate_round_trip_fee(
            entry_price=avg_price,
            exit_price=exit_price,
            quantity=quantity,
            one_side_rate=rate,
        )
        net_expected_profit = expected_profit - estimated_fees
        if net_expected_profit >= required_profit:
            return None
        return self._skip_order(
            symbol,
            action,
            (
                f"net expected profit {net_expected_profit:.2f} after estimated fees "
                f"{estimated_fees:.2f} is below required minimum profit {required_profit:.2f}"
            ),
            skip_category="FEE",
            expected_profit=float(expected_profit),
            estimated_fees=float(estimated_fees),
            net_expected_profit=float(net_expected_profit),
            required_profit=float(required_profit),
            quantity=float(quantity),
            price=float(exit_price),
        )
```

保持既有短路判断 `if allow_loss_exit or quantity <= 0 or avg_price <= 0: return None` 位于费用计算之前。将 `execute()` 中既有拒绝改为走 `_skip_order()`：

```python
        if not risk_result.approved:
            return self._skip_order(symbol, action, risk_result.reason, skip_category="RISK")

        with self._state_lock:
            if self._pending_order is not None:
                return self._skip_order(
                    symbol, action, "pending order in flight", skip_category="PENDING"
                )
```

在无可用 long/short 数量的两个 `_skip_order()` 调用加入：

```python
            skip_category="POSITION",
```

- [ ] **Step 6: 从 runner 将运行时市场费率传入真实执行**

在 `backend/app/runner.py` 导入费用选择函数：

```python
from app.core.fees import one_side_fee_rate
```

新增私有方法：

```python
    def _live_fee_rate(self) -> Decimal:
        return one_side_fee_rate(
            self.engine.params.market,
            Decimal(str(self.engine.params.fee_rate_us)),
            Decimal(str(self.engine.params.fee_rate_hk)),
        )
```

在 `_on_quote()` 和 `_execute_llm_trade_action()` 两处 `self._trade_svc.execute` 调用加入：

```python
                    fee_rate=self._live_fee_rate(),
```

- [ ] **Step 7: 运行费用与执行测试，确认通过**

Run:

```bash
cd backend && python3 -m pytest tests/test_fees.py tests/test_trade_execution_service.py tests/test_trade_event_sync.py tests/test_runner.py -v
```

Expected: PASS。

- [ ] **Step 8: 提交费用保护**

```bash
git add backend/app/core/fees.py backend/app/services/trade_execution_service.py backend/app/runner.py backend/tests/test_fees.py backend/tests/test_trade_execution_service.py backend/tests/test_trade_event_sync.py backend/tests/test_runner.py
git commit -m "feat: apply fee-aware live exit protection"
```

---

### Task 3: LLM 撤单前改价与冷却 Gate

**Files:**
- Modify: `backend/app/services/trade_execution_service.py`
- Modify: `backend/app/runner.py`
- Test: `backend/tests/test_runner.py`

- [ ] **Step 1: 为 pending 只读访问、改价阻断与冷却阻断编写失败测试**

在 `backend/tests/test_runner.py::TestAppRunner` 添加：

```python
    def _runner_with_pending_buy(self, price: Decimal) -> AppRunner:
        from app.core.broker import OrderStatusResult

        class Broker:
            def __init__(self) -> None:
                self.cancelled: list[str] = []

            def cancel_order(self, order_id: str) -> OrderStatusResult:
                self.cancelled.append(order_id)
                return OrderStatusResult(order_id, "CANCELLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.broker = Broker()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._track_pending_order(
            "BUY",
            OrderResult("order-pending", "NVDA.US", "BUY", Decimal("5"), price, "SUBMITTED"),
            runner.broker,
            runner._engine_snapshot(),
        )
        return runner

    def test_llm_cancel_replace_below_repricing_threshold_preserves_pending(self) -> None:
        runner = self._runner_with_pending_buy(price=Decimal("221.00"))
        runner.engine.params.min_repricing_pct = 0.003
        skipped: list[tuple] = []
        runner._record_order_skipped = lambda *args: skipped.append(args)

        result = runner.execute_llm_order_decision({
            "order_action": "CANCEL_REPLACE",
            "replacement_action": "BUY_NOW",
            "replacement_price": 221.20,
        })

        assert result["status"] == "SKIPPED"
        assert runner.broker.cancelled == []
        assert runner._trade_svc.has_pending_order is True
        assert skipped[0][3]["skip_category"] == "REPRICING"

    def test_llm_cooldown_rejection_preserves_pending_before_cancel(self, monkeypatch) -> None:
        runner = self._runner_with_pending_buy(price=Decimal("221.00"))
        runner.engine.params.llm_action_cooldown_seconds = 60
        runner._last_llm_action_at[("NVDA.US", "BUY")] = 100.0
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 120.0)

        result = runner.execute_llm_order_decision({
            "order_action": "BUY_NOW",
            "order_price": 222.00,
        })

        assert result["status"] == "SKIPPED"
        assert runner.broker.cancelled == []
        assert runner._trade_svc.has_pending_order is True

    def test_successful_llm_submission_records_broker_side_cooldown(self, monkeypatch) -> None:
        class Broker:
            def get_quote(self, symbol: str) -> Quote:
                return Quote(symbol, 222.0, 221.9, 222.1, "")

            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult("order-llm-buy", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 100.0)
        result = runner.execute_llm_order_decision({"order_action": "BUY_NOW", "order_price": 221.75})
        assert result["executed"] is True
        assert runner._last_llm_action_at[("NVDA.US", "BUY")] == 100.0
```

将已有 `test_execute_llm_order_decision_cancel_replace` 保留为价格变化足够时的通过用例，并将已有隐式替换用例同样断言通过 gate 后才调用 `cancel_order()`。

- [ ] **Step 2: 运行 runner LLM 用例，确认失败**

Run:

```bash
cd backend && python3 -m pytest tests/test_runner.py -k "llm or cancel_replace or replaces_pending" -v
```

Expected: FAIL，提示尚无 `_last_llm_action_at`、pending 访问器或 gate，且当前代码会先调用撤单。

- [ ] **Step 3: 为 TradeExecutionService 提供安全的 pending 快照访问器**

在 `backend/app/services/trade_execution_service.py` 中，紧接 `has_pending_order` 属性新增：

```python
    @property
    def pending_order(self) -> _PendingOrder | None:
        with self._state_lock:
            return self._pending_order
```

- [ ] **Step 4: 在 AppRunner 增加 LLM gate helper**

在 `AppRunner.__init__()` 初始化：

```python
        self._last_llm_action_at: dict[tuple[str, str], float] = {}
```

将现有 import 改为导入 pending 类型，并在 `backend/app/runner.py` 增加 helper：

```python
from app.services.trade_execution_service import TradeExecutionService, _PendingOrder
```

```python
    @staticmethod
    def _broker_side_for_action(action: str) -> str:
        return "BUY" if action in {"BUY", "BUY_TO_COVER"} else "SELL"

    def _skip_llm_action(self, action: str, reason: str, **payload: object) -> dict[str, Any]:
        symbol = self.engine.params.symbol
        self._record_order_skipped(symbol, action, reason, payload)
        return {"executed": False, "status": "SKIPPED", "order_id": None, "action": action}

    def _precheck_llm_action(
        self,
        action: str,
        proposed_price: Any,
        pending: _PendingOrder | None,
    ) -> dict[str, Any] | None:
        symbol = self.engine.params.symbol
        side = self._broker_side_for_action(action)
        if pending is not None and self.engine.params.min_repricing_pct > 0:
            normalized_price = self._coerce_positive_float(proposed_price)
            if normalized_price is None:
                return {"executed": False, "status": "NO_QUOTE", "order_id": None, "action": action}
            old_price = pending.price
            new_price = Decimal(str(normalized_price))
            repricing_pct = abs(new_price - old_price) / old_price
            if repricing_pct < Decimal(str(self.engine.params.min_repricing_pct)):
                return self._skip_llm_action(
                    action,
                    "replacement price movement is below minimum threshold",
                    skip_category="REPRICING",
                    old_price=float(old_price),
                    new_price=float(new_price),
                    repricing_pct=float(repricing_pct),
                )
        cooldown = self.engine.params.llm_action_cooldown_seconds
        last_at = self._last_llm_action_at.get((symbol, side))
        if cooldown > 0 and last_at is not None:
            remaining = cooldown - (time.monotonic() - last_at)
            if remaining > 0:
                return self._skip_llm_action(
                    action,
                    "LLM action remains in cooldown",
                    skip_category="COOLDOWN",
                    cooldown_remaining_seconds=remaining,
                )
        return None
```

在失败测试中另增以下断言，固定无效替代价格不撤 pending 的行为：

```python
    def test_llm_cancel_replace_without_valid_price_preserves_pending(self) -> None:
        runner = self._runner_with_pending_buy(price=Decimal("221.00"))

        result = runner.execute_llm_order_decision({
            "order_action": "CANCEL_REPLACE",
            "replacement_action": "BUY_NOW",
            "replacement_price": None,
        })

        assert result["status"] == "NO_QUOTE"
        assert runner.broker.cancelled == []
        assert runner._trade_svc.has_pending_order is True
```

- [ ] **Step 5: 在撤单前调用 gate，并在成功提交后记录冷却**

在 `execute_llm_order_decision()` 的 `if action in {"CANCEL_PENDING", "CANCEL_REPLACE"}:` 分支中，删除原有“进入分支立即 `cancel_pending_order`”逻辑，替换为以下次序：

```python
                if action == "CANCEL_PENDING":
                    cancel_status = self._trade_svc.cancel_pending_order(
                        restore_engine_snapshot=self._restore_engine_snapshot,
                    )
                    return {
                        "executed": cancel_status.status == "CANCELLED",
                        "status": cancel_status.status,
                        "order_id": cancel_status.broker_order_id or None,
                        "action": "CANCEL_PENDING",
                    }
                replacement_action = str(decision.get("replacement_action") or "NONE").upper()
                mapped_action = _LLM_ORDER_ACTION_MAP.get(replacement_action)
                if mapped_action is None:
                    return {"executed": False, "status": "UNKNOWN_ACTION", "order_id": None, "action": "CANCEL_REPLACE"}
                proposed_price = decision.get("replacement_price") or decision.get("order_price")
                pending = self._trade_svc.pending_order
                skipped = self._precheck_llm_action(mapped_action, proposed_price, pending)
                if skipped is not None:
                    return skipped
                cancel_status = self._trade_svc.cancel_pending_order(
                    restore_engine_snapshot=self._restore_engine_snapshot,
                )
                if cancel_status.status not in {"CANCELLED", "NO_PENDING_ORDER"}:
                    return {
                        "executed": False,
                        "status": cancel_status.status,
                        "order_id": cancel_status.broker_order_id or None,
                        "action": "CANCEL_REPLACE",
                    }
                return self._execute_llm_trade_action(
                    mapped_action,
                    proposed_price,
                    allow_loss_exit=replacement_action in _LLM_STOP_LOSS_ACTIONS,
                )
```

对非 `CANCEL_*` 的 mapped action，在原有 `if self._trade_svc.has_pending_order:` 判断之前加入 gate；它同时覆盖无 pending 的新 LLM 下单与存在 pending 的隐式替换：

```python
            pending = self._trade_svc.pending_order
            skipped = self._precheck_llm_action(mapped_action, decision.get("order_price"), pending)
            if skipped is not None:
                return skipped
```

因此无 pending 的 LLM 新单也受 cooldown 保护，而 `CANCEL_PENDING` 保持直接撤销、不受 gate 影响。

在 `_execute_llm_trade_action()` 得到成功结果后记录：

```python
        result = {
            "executed": order_status.status in {"FILLED", "SUBMITTED", "PARTIAL_FILLED"},
            "status": order_status.status,
            "order_id": order_status.broker_order_id or None,
            "action": action,
        }
        if result["executed"]:
            side = self._broker_side_for_action(action)
            self._last_llm_action_at[(symbol, side)] = time.monotonic()
        return result
```

- [ ] **Step 6: 运行 LLM runner 测试，确认通过**

Run:

```bash
cd backend && python3 -m pytest tests/test_runner.py -k "llm or cancel_replace or replaces_pending" -v
```

Expected: PASS；低改价和冷却场景均不调用 `cancel_order()`。

- [ ] **Step 7: 提交 LLM gates**

```bash
git add backend/app/services/trade_execution_service.py backend/app/runner.py backend/tests/test_runner.py
git commit -m "feat: guard LLM replacement orders before cancellation"
```

---

### Task 4: 回测跳过原因分类（保持独立费用模型）

**Files:**
- Modify: `backend/app/core/backtest.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/api/backtest.py`
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/utils/labels.ts`
- Modify: `frontend/src/views/Backtest.vue`
- Test: `backend/tests/test_backtest.py`
- Test: `frontend/cypress/e2e/backtest.cy.ts`
- Modify: `frontend/cypress/support/e2e.ts`

- [ ] **Step 1: 编写回测分类的失败测试**

在 `backend/tests/test_backtest.py::test_min_profit_amount_filters_exit_signal` 加入：

```python
        assert result.skipped_signals[0].category == "FEE"
```

在 `backend/tests/test_backtest.py::TestBacktestAPI` 加入完整 API 用例：

```python
    def test_run_backtest_endpoint_returns_cost_skip_category(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "buy_low": 100,
                "sell_high": 101,
                "min_profit_amount": 5,
                "fee_rate": 0.001,
            },
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T10:00:00Z,100,100,99,100,1000\n"
                "2026-05-22T10:01:00Z,100,101.5,100,101,1000\n"
            ),
        })

        assert resp.status_code == 200
        assert resp.json()["skipped_signals"][0]["category"] == "FEE"
```

在 `frontend/cypress/support/e2e.ts` 的 `runBacktest` stub 增加：

```typescript
skipped_signals: [{
  timestamp: '2026-05-22T10:02:00Z',
  action: 'SELL',
  price: 101,
  reason: 'net profit below min_profit_amount',
  state: 'long',
  category: 'FEE',
}],
```

在 `frontend/cypress/e2e/backtest.cy.ts` 的运行用例中加入：

```typescript
    cy.contains('成本不足').should('be.visible')
```

- [ ] **Step 2: 运行后端测试和 Cypress 用例，确认失败**

Run:

```bash
cd backend && python3 -m pytest tests/test_backtest.py -v
cd ../frontend && npx cypress run --spec cypress/e2e/backtest.cy.ts
```

Expected: 后端 FAIL（`BacktestSkippedSignal` 无 `category`）；前端 FAIL（无分类展示）。

- [ ] **Step 3: 加入后端分类字段，不读取实盘配置**

在 `backend/app/core/backtest.py::BacktestSkippedSignal` 增加：

```python
    category: str | None = None
```

为退出净收益不满足门槛的构造加入：

```python
                            category="FEE",
```

为风控暂停造成的 entry skip 加入：

```python
                            category="RISK",
```

在 `backend/app/schemas.py::BacktestSkippedSignal` 增加：

```python
    category: Optional[str] = None
```

在 `backend/app/api/backtest.py` 映射中增加：

```python
                category=signal.category,
```

不得把 `StrategyConfig.fee_rate_us` 或 `fee_rate_hk` 传入 `BacktestEngineParams`。

- [ ] **Step 4: 加入前端分类显示**

在 `frontend/src/types/index.ts::BacktestSkippedSignal` 增加：

```typescript
  category?: string | null
```

在 `frontend/src/utils/labels.ts` 新增共享 helper（Task 6 直接复用）：

```typescript
export function skipCategoryLabel(category?: string | null): string {
  switch (category) {
    case 'FEE': return '成本不足'
    case 'REPRICING': return '改价不显著'
    case 'COOLDOWN': return 'LLM 冷却中'
    case 'RISK': return '风控阻断'
    case 'PENDING': return '已有挂单'
    case 'POSITION': return '可用持仓不足'
    default: return ''
  }
}
```

在 `frontend/src/views/Backtest.vue` 导入 `skipCategoryLabel`，在 skip row 中加入：

```vue
<el-tag v-if="signal.category" size="small" type="warning" effect="plain">
  {{ skipCategoryLabel(signal.category) }}
</el-tag>
```

- [ ] **Step 5: 运行回测测试，确认通过**

Run:

```bash
cd backend && python3 -m pytest tests/test_backtest.py -v
cd ../frontend && npx cypress run --spec cypress/e2e/backtest.cy.ts
```

Expected: PASS。

- [ ] **Step 6: 提交回测分类**

```bash
git add backend/app/core/backtest.py backend/app/schemas.py backend/app/api/backtest.py backend/tests/test_backtest.py frontend/src/types/index.ts frontend/src/views/Backtest.vue frontend/src/utils/labels.ts frontend/cypress/support/e2e.ts frontend/cypress/e2e/backtest.cy.ts
git commit -m "feat: classify cost-based backtest skips"
```

---

### Task 5: Strategy 配置页面暴露执行保护设置

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/composables/useDashboardData.ts`
- Modify: `frontend/src/views/Strategy.vue`
- Modify: `frontend/cypress/support/e2e.ts`
- Test: `frontend/cypress/e2e/strategy.cy.ts`

- [ ] **Step 1: 编写表单字段与保存 payload 的失败 Cypress 测试**

在 `frontend/cypress/e2e/strategy.cy.ts` 新增：

```typescript
  it('edits cost and LLM execution protection settings', () => {
    cy.intercept('PUT', '/api/strategy', (req) => {
      expect(req.body.fee_rate_us).to.equal(0.001)
      expect(req.body.fee_rate_hk).to.equal(0.004)
      expect(req.body.min_repricing_pct).to.equal(0.005)
      expect(req.body.llm_action_cooldown_seconds).to.equal(120)
      req.reply({ statusCode: 200, body: Object.assign({ id: 1, updated_at: '2026-05-25T00:00:00Z' }, req.body) })
    }).as('saveSafetySettings')

    cy.contains('美股单边预估费率').parent().find('input').clear().type('0.10')
    cy.contains('港股单边预估费率').parent().find('input').clear().type('0.40')
    cy.contains('LLM 最小改价').parent().find('input').clear().type('0.50')
    cy.contains('LLM 同向冷却').parent().find('input').clear().type('120')
    cy.contains('保存').click()
    cy.wait('@saveSafetySettings')
  })
```

在 `frontend/cypress/support/e2e.ts` 的 `GET /api/strategy` stub 增加：

```typescript
      fee_rate_us: 0.0005, fee_rate_hk: 0.003,
      min_repricing_pct: 0.003, llm_action_cooldown_seconds: 60,
```

- [ ] **Step 2: 运行策略 Cypress，用新增测试确认失败**

Run:

```bash
cd frontend && npx cypress run --spec cypress/e2e/strategy.cy.ts
```

Expected: FAIL，页面尚无四项设置输入。

- [ ] **Step 3: 扩展前端策略类型与默认数据**

在 `frontend/src/types/index.ts::StrategyConfig` 和 `frontend/src/views/Strategy.vue::StrategyForm` 增加：

```typescript
  fee_rate_us: number
  fee_rate_hk: number
  min_repricing_pct: number
  llm_action_cooldown_seconds: number
```

在 `frontend/src/composables/useDashboardData.ts` 的 API 比例默认值加入：

```typescript
fee_rate_us: 0.0005,
fee_rate_hk: 0.003,
min_repricing_pct: 0.003,
llm_action_cooldown_seconds: 60,
```

在 `Strategy.vue` 表单 initial 中使用百分数显示默认值：

```typescript
fee_rate_us: 0.05,
fee_rate_hk: 0.30,
min_repricing_pct: 0.30,
llm_action_cooldown_seconds: 60,
```

在 Strategy 表单 `load` 中将 API 比例转换为百分数：

```typescript
// API stores ratio; UI displays percentage.
fee_rate_us: s.fee_rate_us * 100,
fee_rate_hk: s.fee_rate_hk * 100,
min_repricing_pct: s.min_repricing_pct * 100,
```

在 Strategy 表单 `save` 中将变更后的百分数转回 API 比例：

```typescript
if (!previous || data.fee_rate_us !== previous.fee_rate_us) patch.fee_rate_us = data.fee_rate_us / 100
if (!previous || data.fee_rate_hk !== previous.fee_rate_hk) patch.fee_rate_hk = data.fee_rate_hk / 100
if (!previous || data.min_repricing_pct !== previous.min_repricing_pct) patch.min_repricing_pct = data.min_repricing_pct / 100
if (!previous || data.llm_action_cooldown_seconds !== previous.llm_action_cooldown_seconds) {
  patch.llm_action_cooldown_seconds = data.llm_action_cooldown_seconds
}
```

- [ ] **Step 4: 在 Strategy 表单增加成本与执行保护区域**

在现有策略表单的 `min_profit_amount` 后添加：

```vue
        <el-divider content-position="left">成本与执行保护</el-divider>
        <el-form-item label="美股单边预估费率（%）">
          <el-input-number v-model="form.fee_rate_us" :min="0" :max="1" :precision="3" :step="0.01" />
        </el-form-item>
        <el-form-item label="港股单边预估费率（%）">
          <el-input-number v-model="form.fee_rate_hk" :min="0" :max="2" :precision="3" :step="0.01" />
        </el-form-item>
        <el-form-item label="LLM 最小改价（%）">
          <el-input-number v-model="form.min_repricing_pct" :min="0" :max="5" :precision="3" :step="0.01" />
        </el-form-item>
        <el-form-item label="LLM 同向冷却（秒）">
          <el-input-number v-model="form.llm_action_cooldown_seconds" :min="0" :max="3600" :step="1" />
        </el-form-item>
```

- [ ] **Step 5: 运行 type-check 与 Strategy Cypress，确认通过**

Run:

```bash
cd frontend && npm run type-check && npx cypress run --spec cypress/e2e/strategy.cy.ts
```

Expected: PASS。

- [ ] **Step 6: 提交策略 UI**

```bash
git add frontend/src/types/index.ts frontend/src/composables/useDashboardData.ts frontend/src/views/Strategy.vue frontend/cypress/support/e2e.ts frontend/cypress/e2e/strategy.cy.ts
git commit -m "feat: expose execution protection strategy settings"
```

---

### Task 6: Decision Timeline 与 Dashboard 展示跳过原因

**Files:**
- Modify: `frontend/src/utils/labels.ts`
- Modify: `frontend/src/views/DecisionTimeline.vue`
- Modify: `frontend/src/views/Dashboard.vue`
- Modify: `frontend/cypress/support/e2e.ts`
- Test: `frontend/cypress/e2e/events.cy.ts`
- Test: `frontend/cypress/e2e/dashboard.cy.ts`

- [ ] **Step 1: 编写原因显示和筛选失败测试**

把 `frontend/cypress/support/e2e.ts` 中 `ORDER_SKIPPED` payload 扩为：

```typescript
payload: { skip_category: 'FEE', expected_profit: 4, estimated_fees: 1, required_profit: 5 },
```

在 `frontend/cypress/e2e/events.cy.ts` 新增：

```typescript
  it('shows and filters skipped order categories', () => {
    cy.visitApp('/#/events')
    cy.contains('成本不足').should('be.visible')
    cy.get('[data-testid="skip-category-filter"]').click()
    cy.contains('.el-select-dropdown__item', '成本不足').click()
    cy.contains('订单跳过').should('be.visible')
    cy.contains('LLM 分析').should('not.exist')
  })
```

在 `frontend/cypress/e2e/dashboard.cy.ts` 加入：

```typescript
  it('shows the skipped order category in recent events', () => {
    cy.visitApp('/')
    cy.get('[data-testid="recent-events"]').should('contain', '成本不足')
  })
```

- [ ] **Step 2: 运行两个 Cypress spec，确认失败**

Run:

```bash
cd frontend && npx cypress run --spec "cypress/e2e/events.cy.ts,cypress/e2e/dashboard.cy.ts"
```

Expected: FAIL，页面尚未渲染或筛选 `skip_category`。

- [ ] **Step 3: 复用共享 label helper**

确认 Task 4 已在 `frontend/src/utils/labels.ts` 提供：

```typescript
export function skipCategoryLabel(category?: string | null): string {
  switch (category) {
    case 'FEE': return '成本不足'
    case 'REPRICING': return '改价不显著'
    case 'COOLDOWN': return 'LLM 冷却中'
    case 'RISK': return '风控阻断'
    case 'PENDING': return '已有挂单'
    case 'POSITION': return '可用持仓不足'
    default: return ''
  }
}
```

- [ ] **Step 4: 实现 Decision Timeline 当前页筛选**

在 `frontend/src/views/DecisionTimeline.vue`：

```typescript
import { computed, onMounted, ref } from 'vue'
import { orderSideLabel, skipCategoryLabel, tradeEventTypeLabel } from '../utils/labels'

const selectedSkipCategory = ref('')
const visibleEvents = computed(() => {
  if (!selectedSkipCategory.value) return events.value
  return events.value.filter((event) => event.payload.skip_category === selectedSkipCategory.value)
})
```

表头 actions 前加入：

```vue
<el-select
  v-model="selectedSkipCategory"
  clearable
  placeholder="跳过原因"
  data-testid="skip-category-filter"
  style="width: 150px"
>
  <el-option label="成本不足" value="FEE" />
  <el-option label="改价不显著" value="REPRICING" />
  <el-option label="LLM 冷却中" value="COOLDOWN" />
  <el-option label="风控阻断" value="RISK" />
  <el-option label="已有挂单" value="PENDING" />
  <el-option label="可用持仓不足" value="POSITION" />
</el-select>
```

将 table 数据改成 `:data="visibleEvents"`，并增加分类列：

```vue
<el-table-column label="原因分类" min-width="120">
  <template #default="{ row }">
    <el-tag v-if="row.payload.skip_category" type="warning" effect="plain">
      {{ skipCategoryLabel(String(row.payload.skip_category)) }}
    </el-tag>
    <span v-else>-</span>
  </template>
</el-table-column>
```

- [ ] **Step 5: 在 Dashboard 显示最近跳过原因**

在 `frontend/src/views/Dashboard.vue` 导入 `skipCategoryLabel`，并在 event message 下添加：

```vue
<small
  v-if="event.event_type === 'ORDER_SKIPPED' && event.payload.skip_category"
  class="skip-category"
>
  {{ skipCategoryLabel(String(event.payload.skip_category)) }}
</small>
```

- [ ] **Step 6: 运行 type-check 和 Cypress，确认通过**

Run:

```bash
cd frontend && npm run type-check && npx cypress run --spec "cypress/e2e/events.cy.ts,cypress/e2e/dashboard.cy.ts"
```

Expected: PASS。

- [ ] **Step 7: 提交跳过原因 UI**

```bash
git add frontend/src/utils/labels.ts frontend/src/views/DecisionTimeline.vue frontend/src/views/Dashboard.vue frontend/cypress/support/e2e.ts frontend/cypress/e2e/events.cy.ts frontend/cypress/e2e/dashboard.cy.ts
git commit -m "feat: display skipped order categories"
```

---

### Task 7: 用户文档与全量交付验证

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/Roadmap.md`

- [ ] **Step 1: 更新用户说明与维护约束**

在 `README.md` 的策略/风控说明增加已经交付的行为：

```markdown
- 普通平仓会按市场配置的单边预估费率计算双边费用；费用后预期收益仍需达到最低盈利门槛
- LLM 撤单重挂只有在新旧价格差达到配置阈值时才执行；重复同方向 LLM 发单受独立冷却限制
- Decision Timeline / Dashboard 会展示订单被跳过的原因分类
```

在策略配置表加入：

```markdown
| US Estimated Fee Rate | 美股单边预估费率，用于实盘普通平仓成本保护 | `0.05%` |
| HK Estimated Fee Rate | 港股单边预估费率，用于实盘普通平仓成本保护 | `0.30%` |
| LLM Repricing Threshold | LLM 撤单重挂的最小改价幅度 | `0.30%` |
| LLM Action Cooldown | LLM 同方向发单冷却时间 | `60s` |
```

在 `CLAUDE.md` 的交易执行约束补充新增字段、费用保护和撤单前 gate；在 `docs/Roadmap.md` 将 P4 标为完成并写入本轮验证结果。

- [ ] **Step 2: 运行后端全量测试与类型检查**

Run:

```bash
cd backend && python3 -m pytest tests/ -v
cd .. && python3 -m basedpyright
```

Expected: pytest 无失败；basedpyright 报 `0 errors`。

- [ ] **Step 3: 运行前端类型检查、构建与相关 E2E**

Run:

```bash
cd frontend && npm run type-check && npm run build
npx cypress run --spec "cypress/e2e/strategy.cy.ts,cypress/e2e/events.cy.ts,cypress/e2e/dashboard.cy.ts,cypress/e2e/backtest.cy.ts"
```

Expected: type-check/build 成功且四个 Cypress specs 全部通过。

- [ ] **Step 4: 复核实现边界**

Run:

```bash
rg -n "fee_rate_us|fee_rate_hk|min_repricing_pct|llm_action_cooldown_seconds|skip_category" backend/app frontend/src README.md CLAUDE.md docs/Roadmap.md
rg -n "fee_rate_us|fee_rate_hk" backend/app/core/backtest.py backend/app/api/backtest.py
git diff --stat
```

Expected: 第一条能定位配置、执行、UI 与文档；第二条无输出，证明实盘费率未泄漏进回测模型；diff 仅包含 P4 预期文件。

- [ ] **Step 5: 提交交付文档**

```bash
git add README.md CLAUDE.md docs/Roadmap.md
git commit -m "docs: document trade execution safety controls"
```

---

## 实施顺序与验收摘要

1. Task 1 先让数据库、API 与运行时都持有配置，后续逻辑不依赖临时常量。
2. Task 2 将普通退出的实盘费用保护固定在 `TradeExecutionService`，并使旧跳过路径可观察。
3. Task 3 只在 LLM 路径新增撤单前 gate，不改变行情触发策略时序。
4. Task 4 明确回测只补分类、不接入实盘费率。
5. Task 5-6 把配置和执行结果暴露给用户。
6. Task 7 在所有功能可验证之后更新用户向文档与 Roadmap 完成状态。

实现完成时必须满足：

- 低于改价阈值或处于 LLM 冷却的替换动作不会撤销当前 pending order。
- 止损退出不会被费用门槛拦截。
- 回测仍使用用户输入的 `fee_rate` / `fixed_fee` / `slippage_pct`。
- P2 不被作为附带改动重新引入。
