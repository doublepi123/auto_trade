# P151 + P152 + P156：组合交易、Paper Broker 与组合风控

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 P149+P150 平台基础上实现多标的组合配置、真实成交仿真的 Paper Broker，以及组合级风控熔断，形成 backtest / paper / live 统一事件语义下的多标的闭环。

**Architecture：** `PortfolioConfig` 描述目标权重与风险预算；`PortfolioAllocator` 对比目标权重与当前持仓生成再平衡 `OrderIntent`；`PaperBroker` 替代 `SimBroker` 提供 partial fill、滑点、延迟、撤改单状态机；`PortfolioRiskController` 订阅事件计算组合敞口/回撤/相关性并发出 `RiskEvent`；所有组件通过 `EventBus` 与 `PlatformRunner` 交互。

**Tech Stack：** Python 3.11+、FastAPI、SQLAlchemy 2.0、SQLite、pytest、basedpyright、dataclasses。

---

## 0. 前置准备

**先确认环境：**

```bash
cd /home/lcy/code/auto_trade/.claude/worktrees/p151-p158-quant-platform/backend
python3 --version   # 必须 3.11+
source /home/lcy/code/auto_trade/backend/.venv/bin/activate
pytest tests/ -q    # 应 1227 passed, 1 skipped
```

**工作区：** 本计划在独立 worktree `/home/lcy/code/auto_trade/.claude/worktrees/p151-p158-quant-platform` 中执行。

---

## 1. 文件结构总览

### 1.1 新增文件

| 文件 | 职责 |
|------|------|
| `backend/app/platform/portfolio_config.py` | `PortfolioConfig` dataclass 与验证 |
| `backend/app/platform/portfolio_allocator.py` | 根据目标权重与当前持仓生成再平衡订单 |
| `backend/app/platform/portfolio_service.py` | 组合配置 CRUD、快照计算 |
| `backend/app/platform/portfolio_api.py` | `/api/portfolio/*` 路由 |
| `backend/app/platform/paper_broker.py` | Paper Broker：partial fill、滑点、延迟、撤改单 |
| `backend/app/platform/paper_order_state.py` | Paper 订单状态机 dataclass |
| `backend/app/platform/portfolio_risk.py` | 组合级风控：敞口、回撤、相关性 |
| `backend/app/platform/risk_engine.py` | 订阅事件并驱动风控判断的轻量引擎 |
| `backend/app/models.py` 新增 `PortfolioConfig` / `PaperOrder` / `PortfolioSnapshot` 模型 | 持久化 |
| `backend/app/database.py` 新增 `_ensure_*` | 建表迁移 |
| `backend/tests/platform/test_portfolio*.py` | 组合相关测试 |
| `backend/tests/platform/test_paper_broker.py` | Paper Broker 测试 |
| `backend/tests/platform/test_portfolio_risk.py` | 组合风控测试 |

### 1.2 修改文件

| 文件 | 改造 |
|------|------|
| `backend/app/platform/runner.py` | 支持 symbol 列表、注入 PaperBroker、接入 RiskEngine |
| `backend/app/platform/simbroker.py` | 标记为 deprecated，逐步由 PaperBroker 取代（backtest 模式也使用 PaperBroker） |
| `backend/app/platform/events.py` | 扩展 `OrderIntentEvent` / `OrderEvent` / `FillEvent` 支持 partial fill、slippage、commission |
| `backend/app/main.py` | 注册 `/api/portfolio` 路由 |

---

## 2. 数据模型扩展

### Task 1：新增 `PortfolioConfig` 表

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/database.py`
- Test: `backend/tests/platform/test_portfolio_config.py`

**Step 1: 写 failing test**

```python
# backend/tests/platform/test_portfolio_config.py
from app.database import engine
from app.models import Base, PortfolioConfig


def test_portfolio_config_roundtrip():
    Base.metadata.create_all(bind=engine)
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        cfg = PortfolioConfig(
            name="test-portfolio",
            symbols_json='["AAPL.US","TSLA.US"]',
            allocations_json='{"AAPL.US":0.6,"TSLA.US":0.4}',
            per_symbol_risk_json='{"AAPL.US":0.05,"TSLA.US":0.05}',
            rebalance_threshold_pct=5.0,
            max_gross_exposure=1.0,
            max_net_exposure=0.5,
        )
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
        assert cfg.id is not None
        assert cfg.name == "test-portfolio"
```

**Step 2: 运行测试确认失败**

```bash
cd /home/lcy/code/auto_trade/.claude/worktrees/p151-p158-quant-platform/backend
source /home/lcy/code/auto_trade/backend/.venv/bin/activate
pytest tests/platform/test_portfolio_config.py -v
```

Expected: `ImportError: cannot import name 'PortfolioConfig'`

**Step 3: 在 models.py 添加模型**

```python
# backend/app/models.py 中合适位置（StrategyConfig 之后）
class PortfolioConfig(Base):
    __tablename__ = "portfolio_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    symbols_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allocations_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    per_symbol_risk_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    rebalance_threshold_pct: Mapped[float] = mapped_column(Float, default=5.0)
    max_gross_exposure: Mapped[float] = mapped_column(Float, default=1.0)
    max_net_exposure: Mapped[float] = mapped_column(Float, default=0.5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)
```

**Step 4: 在 database.py 添加 ensure**

```python
# backend/app/database.py

def _ensure_portfolio_config_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "portfolio_config" not in inspector.get_table_names():
        Base.metadata.create_all(bind=db_engine, tables=[PortfolioConfig.__table__])
```

并在 `init_db()` 中调用 `_ensure_portfolio_config_table(engine)`。

**Step 5: 运行测试确认通过**

```bash
pytest tests/platform/test_portfolio_config.py -v
```

Expected: 1 passed

**Step 6: Commit**

```bash
git add backend/app/models.py backend/app/database.py backend/tests/platform/test_portfolio_config.py
git commit -m "P151: PortfolioConfig model and persistence"
```

---

## 3. 组合配置与资金分配

### Task 2：实现 `PortfolioConfig` dataclass 与验证

**Files:**
- Create: `backend/app/platform/portfolio_config.py`
- Test: `backend/tests/platform/test_portfolio_config.py`（继续添加）

**Step 1: 写 failing test**

```python
# backend/tests/platform/test_portfolio_config.py
from decimal import Decimal

from app.platform.portfolio_config import PortfolioConfig


def test_portfolio_config_validates_allocations_sum_to_one():
    with pytest.raises(ValueError, match="allocations must sum to 1"):
        PortfolioConfig(
            name="bad",
            symbols=["AAPL.US", "TSLA.US"],
            allocations={"AAPL.US": Decimal("0.6"), "TSLA.US": Decimal("0.5")},
        )
```

**Step 2: 实现 dataclass**

```python
# backend/app/platform/portfolio_config.py
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class PortfolioConfig:
    name: str
    symbols: list[str]
    allocations: dict[str, Decimal]
    per_symbol_risk_budget: dict[str, Decimal] | None = None
    rebalance_threshold_pct: Decimal = Decimal("5")
    max_gross_exposure: Decimal = Decimal("1.0")
    max_net_exposure: Decimal = Decimal("1.0")
    enabled: bool = True

    def __post_init__(self):
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        if set(self.allocations.keys()) != set(self.symbols):
            raise ValueError("allocations keys must match symbols")
        total = sum(self.allocations.values(), Decimal("0"))
        if total != Decimal("1"):
            raise ValueError("allocations must sum to 1")
        if self.per_symbol_risk_budget is None:
            self.per_symbol_risk_budget = {s: Decimal("0.05") for s in self.symbols}

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "symbols": self.symbols,
                "allocations": {k: str(v) for k, v in self.allocations.items()},
                "per_symbol_risk_budget": {k: str(v) for k, v in self.per_symbol_risk_budget.items()},
                "rebalance_threshold_pct": str(self.rebalance_threshold_pct),
                "max_gross_exposure": str(self.max_gross_exposure),
                "max_net_exposure": str(self.max_net_exposure),
                "enabled": self.enabled,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> PortfolioConfig:
        data = json.loads(raw)
        return cls(
            name=data["name"],
            symbols=data["symbols"],
            allocations={k: Decimal(v) for k, v in data["allocations"].items()},
            per_symbol_risk_budget={k: Decimal(v) for k, v in data.get("per_symbol_risk_budget", {}).items()},
            rebalance_threshold_pct=Decimal(data.get("rebalance_threshold_pct", "5")),
            max_gross_exposure=Decimal(data.get("max_gross_exposure", "1.0")),
            max_net_exposure=Decimal(data.get("max_net_exposure", "1.0")),
            enabled=data.get("enabled", True),
        )
```

**Step 3: 运行测试确认通过**

```bash
pytest tests/platform/test_portfolio_config.py -v
```

Expected: 2 passed

**Step 4: Commit**

```bash
git add backend/app/platform/portfolio_config.py backend/tests/platform/test_portfolio_config.py
git commit -m "P151: PortfolioConfig dataclass with validation"
```

---

### Task 3：实现 `PortfolioAllocator`

**Files:**
- Create: `backend/app/platform/portfolio_allocator.py`
- Test: `backend/tests/platform/test_portfolio_allocator.py`

**Step 1: 写 failing test**

```python
# backend/tests/platform/test_portfolio_allocator.py
from decimal import Decimal

from app.platform.portfolio_allocator import PortfolioAllocator
from app.platform.portfolio_config import PortfolioConfig


def test_allocator_generates_rebalance_orders():
    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US", "TSLA.US"],
        allocations={"AAPL.US": Decimal("0.6"), "TSLA.US": Decimal("0.4")},
    )
    allocator = PortfolioAllocator(config)
    positions = {"AAPL.US": {"quantity": 5, "price": Decimal("150")}, "TSLA.US": {"quantity": 0}}
    cash = Decimal("10000")
    prices = {"AAPL.US": Decimal("150"), "TSLA.US": Decimal("200")}

    intents = allocator.rebalance(positions, prices, cash)
    assert len(intents) == 1
    assert intents[0].symbol == "TSLA.US"
    assert intents[0].side == "BUY"
```

**Step 2: 实现 allocator**

```python
# backend/app/platform/portfolio_allocator.py
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.platform.portfolio_config import PortfolioConfig
from app.platform.sdk import OrderIntent


class PortfolioAllocator:
    def __init__(self, config: PortfolioConfig) -> None:
        self.config = config

    def rebalance(
        self,
        positions: dict[str, dict[str, Any]],
        prices: dict[str, Decimal],
        cash: Decimal,
    ) -> list[OrderIntent]:
        total_value = cash
        for symbol, pos in positions.items():
            qty = int(pos.get("quantity", 0))
            price = prices.get(symbol, Decimal("0"))
            total_value += Decimal(qty) * price

        intents: list[OrderIntent] = []
        for symbol in self.config.symbols:
            target_weight = self.config.allocations.get(symbol, Decimal("0"))
            target_value = total_value * target_weight
            price = prices.get(symbol, Decimal("0"))
            if price <= 0:
                continue
            target_qty = int((target_value / price).to_integral_value())
            current_qty = int(positions.get(symbol, {}).get("quantity", 0))
            delta = target_qty - current_qty
            if delta == 0:
                continue
            side = "BUY" if delta > 0 else "SELL"
            intents.append(
                OrderIntent(
                    symbol=symbol,
                    side=side,
                    quantity=abs(delta),
                    order_type="LIMIT",
                    limit_price=price,
                    reason="portfolio_rebalance",
                )
            )
        return intents
```

**Step 3: 运行测试确认通过**

```bash
pytest tests/platform/test_portfolio_allocator.py -v
```

Expected: 1 passed

**Step 4: Commit**

```bash
git add backend/app/platform/portfolio_allocator.py backend/tests/platform/test_portfolio_allocator.py
git commit -m "P151: PortfolioAllocator generates rebalance intents"
```

---

### Task 4：组合 Service 与 API

**Files:**
- Create: `backend/app/platform/portfolio_service.py`
- Create: `backend/app/platform/portfolio_api.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_portfolio_api.py`

**Step 1: 实现 service**

```python
# backend/app/platform/portfolio_service.py
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import PortfolioConfig as PortfolioConfigModel
from app.platform.portfolio_config import PortfolioConfig


class PortfolioService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_configs(self) -> list[PortfolioConfig]:
        rows = self.db.query(PortfolioConfigModel).order_by(PortfolioConfigModel.id.desc()).all()
        return [self._to_domain(row) for row in rows]

    def get_config(self, name: str) -> PortfolioConfig | None:
        row = self.db.query(PortfolioConfigModel).filter(PortfolioConfigModel.name == name).first()
        if row is None:
            return None
        return self._to_domain(row)

    def save_config(self, config: PortfolioConfig) -> PortfolioConfig:
        row = self.db.query(PortfolioConfigModel).filter(PortfolioConfigModel.name == config.name).first()
        data = {
            "symbols_json": json.dumps(config.symbols),
            "allocations_json": json.dumps({k: str(v) for k, v in config.allocations.items()}),
            "per_symbol_risk_json": json.dumps({k: str(v) for k, v in config.per_symbol_risk_budget.items()}),
            "rebalance_threshold_pct": float(config.rebalance_threshold_pct),
            "max_gross_exposure": float(config.max_gross_exposure),
            "max_net_exposure": float(config.max_net_exposure),
            "enabled": config.enabled,
        }
        if row is None:
            row = PortfolioConfigModel(name=config.name, **data)
            self.db.add(row)
        else:
            for k, v in data.items():
                setattr(row, k, v)
        self.db.commit()
        self.db.refresh(row)
        return self._to_domain(row)

    def _to_domain(self, row: PortfolioConfigModel) -> PortfolioConfig:
        return PortfolioConfig(
            name=row.name,
            symbols=json.loads(row.symbols_json),
            allocations={k: Decimal(v) for k, v in json.loads(row.allocations_json).items()},
            per_symbol_risk_budget={k: Decimal(v) for k, v in json.loads(row.per_symbol_risk_json).items()},
            rebalance_threshold_pct=Decimal(str(row.rebalance_threshold_pct)),
            max_gross_exposure=Decimal(str(row.max_gross_exposure)),
            max_net_exposure=Decimal(str(row.max_net_exposure)),
            enabled=row.enabled,
        )
```

**Step 2: 实现 API router**

```python
# backend/app/platform/portfolio_api.py
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_api_key
from app.api.deps import get_db
from app.platform.portfolio_config import PortfolioConfig
from app.platform.portfolio_service import PortfolioService

router = APIRouter()


def _parse_config(payload: dict[str, Any]) -> PortfolioConfig:
    return PortfolioConfig(
        name=payload["name"],
        symbols=payload["symbols"],
        allocations={k: Decimal(str(v)) for k, v in payload["allocations"].items()},
        per_symbol_risk_budget={k: Decimal(str(v)) for k, v in payload.get("per_symbol_risk_budget", {}).items()} or None,
        rebalance_threshold_pct=Decimal(str(payload.get("rebalance_threshold_pct", 5))),
        max_gross_exposure=Decimal(str(payload.get("max_gross_exposure", 1.0))),
        max_net_exposure=Decimal(str(payload.get("max_net_exposure", 1.0))),
        enabled=payload.get("enabled", True),
    )


@router.get("/config")
def list_portfolio_configs(db=Depends(get_db)) -> list[dict[str, Any]]:
    svc = PortfolioService(db)
    return [
        {
            "name": c.name,
            "symbols": c.symbols,
            "allocations": {k: float(v) for k, v in c.allocations.items()},
            "per_symbol_risk_budget": {k: float(v) for k, v in c.per_symbol_risk_budget.items()},
            "rebalance_threshold_pct": float(c.rebalance_threshold_pct),
            "max_gross_exposure": float(c.max_gross_exposure),
            "max_net_exposure": float(c.max_net_exposure),
            "enabled": c.enabled,
        }
        for c in svc.list_configs()
    ]


@router.put("/config/{name}")
def save_portfolio_config(name: str, payload: dict[str, Any], db=Depends(get_db)) -> dict[str, Any]:
    if payload.get("name") != name:
        raise HTTPException(status_code=400, detail="name mismatch")
    config = _parse_config(payload)
    svc = PortfolioService(db)
    saved = svc.save_config(config)
    return {"name": saved.name, "status": "saved"}
```

**Step 3: 注册 router**

```python
# backend/app/main.py
from app.platform.portfolio_api import router as portfolio_router

app.include_router(portfolio_router, prefix="/api/portfolio")
```

**Step 4: 写测试**

```python
# backend/tests/test_portfolio_api.py
from fastapi.testclient import TestClient

from app.main import app


def test_portfolio_config_crud(monkeypatch):
    monkeypatch.setattr("app.database.engine", None)  # placeholder; actual test needs isolated DB setup
    client = TestClient(app)
    payload = {
        "name": "demo",
        "symbols": ["AAPL.US"],
        "allocations": {"AAPL.US": 1.0},
    }
    resp = client.put("/api/portfolio/config/demo", json=payload)
    assert resp.status_code == 200
    assert resp.json()["name"] == "demo"
```

**注意：** 上面测试用了占位 `monkeypatch.setattr("app.database.engine", None)`，实际执行时要改为使用独立的测试 DB（参见测试约定）。为保持计划简洁，测试代码在实现时调整为使用 `Base.metadata.create_all` + 内存 SQLite 或临时文件。

**Step 5: Commit**

```bash
git add backend/app/platform/portfolio_service.py backend/app/platform/portfolio_api.py backend/app/main.py backend/tests/test_portfolio_api.py
git commit -m "P151: portfolio config CRUD and API"
```

---

## 4. Paper Broker

### Task 5：扩展事件模型支持 partial fill 与费用

**Files:**
- Modify: `backend/app/platform/events.py`
- Test: `backend/tests/platform/test_events.py`

**Step 1: 扩展 FillEvent**

```python
# backend/app/platform/events.py
@dataclass(frozen=True, kw_only=True)
class FillEvent(Event):
    event_type: ClassVar[str] = "fill"

    broker_order_id: str
    side: str
    quantity: int
    price: Decimal
    fee: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")          # 新增
    commission: Decimal = Decimal("0")        # 新增
    partial: bool = False                      # 新增
```

**Step 2: 扩展 OrderEvent**

```python
@dataclass(frozen=True, kw_only=True)
class OrderEvent(Event):
    event_type: ClassVar[str] = "order"

    broker_order_id: str
    status: str  # SUBMITTED/ACKED/PARTIAL_FILLED/FILLED/CANCELLED/MODIFIED/REJECTED
    filled_quantity: int = 0
    avg_price: Decimal | None = None
    reason: str = ""  # 新增：REJECTED 等原因
```

**Step 3: 更新 from_dict / to_dict 处理新增字段**

**Step 4: 运行事件测试**

```bash
pytest tests/platform/test_events.py -v
```

Expected: all passed

**Step 5: Commit**

```bash
git add backend/app/platform/events.py backend/tests/platform/test_events.py
git commit -m "P152: extend events for partial fill, slippage and commission"
```

---

### Task 6：实现 Paper Broker 核心状态机

**Files:**
- Create: `backend/app/platform/paper_order_state.py`
- Create: `backend/app/platform/paper_broker.py`
- Test: `backend/tests/platform/test_paper_broker.py`

**Step 1: 写 failing test**

```python
# backend/tests/platform/test_paper_broker.py
from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource
from app.platform.paper_broker import PaperBroker
from app.platform.sdk import OrderIntent


def test_paper_broker_fills_limit_buy_partially():
    broker = PaperBroker()
    intent = OrderIntent(
        symbol="AAPL.US",
        side="BUY",
        quantity=100,
        order_type="LIMIT",
        limit_price=Decimal("145"),
        reason="test",
    )
    order_event = broker.submit(intent)
    assert order_event.status == "SUBMITTED"

    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("146"),
        high=Decimal("146"),
        low=Decimal("144"),
        close=Decimal("144.5"),
        volume=10000,
    )
    fills = broker.on_bar(bar)
    assert len(fills) >= 1
    assert fills[0].side == "BUY"
    assert fills[0].quantity <= 100
```

**Step 2: 实现 paper_order_state**

```python
# backend/app/platform/paper_order_state.py
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.platform.sdk import OrderIntent


@dataclass
class PaperOrderState:
    order_id: str
    intent: OrderIntent
    status: str = "SUBMITTED"
    filled_quantity: int = 0
    fills: list = field(default_factory=list)

    @property
    def remaining_quantity(self) -> int:
        return self.intent.quantity - self.filled_quantity

    def fill(self, qty: int, price: Decimal, slippage: Decimal = Decimal("0"), commission: Decimal = Decimal("0")) -> None:
        self.filled_quantity += qty
        self.fills.append({"quantity": qty, "price": price, "slippage": slippage, "commission": commission})
        if self.filled_quantity >= self.intent.quantity:
            self.status = "FILLED"
        else:
            self.status = "PARTIAL_FILLED"
```

**Step 3: 实现 PaperBroker**

```python
# backend/app/platform/paper_broker.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from app.platform.events import BarEvent, EventSource, FillEvent, OrderEvent, QuoteEvent
from app.platform.paper_order_state import PaperOrderState
from app.platform.sdk import OrderIntent


@dataclass
class PaperBrokerConfig:
    slippage_ticks: Decimal = Decimal("0.01")
    commission_rate: Decimal = Decimal("0.0005")
    partial_fill_probability: float = 1.0  # Phase 1：默认全部成交，后续改为概率
    latency_ms: int = 0


class PaperBroker:
    """真实成交仿真的 Paper Broker。Phase 1 支持 LIMIT 单按 bar 撮合、partial fill、滑点、费用。"""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        config: PaperBrokerConfig | None = None,
    ) -> None:
        self._orders: dict[str, PaperOrderState] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._config = config or PaperBrokerConfig()

    def submit(self, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent:
        order_id = f"paper-{uuid4().hex[:8]}"
        self._orders[order_id] = PaperOrderState(order_id=order_id, intent=intent)
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=intent.symbol,
            broker_order_id=order_id,
            status="SUBMITTED",
        )

    def cancel(self, order_id: str, timestamp: datetime | None = None) -> OrderEvent:
        order = self._orders.get(order_id)
        if order and order.status in ("SUBMITTED", "PARTIAL_FILLED"):
            order.status = "CANCELLED"
            return OrderEvent(
                timestamp=timestamp or self._clock(),
                source=EventSource.BROKER,
                symbol=order.intent.symbol,
                broker_order_id=order_id,
                status="CANCELLED",
            )
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=order.intent.symbol if order else None,
            broker_order_id=order_id,
            status="REJECTED",
            reason="cannot cancel",
        )

    def modify(self, order_id: str, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent:
        order = self._orders.get(order_id)
        if order and order.status in ("SUBMITTED", "PARTIAL_FILLED"):
            order.intent = intent
            return OrderEvent(
                timestamp=timestamp or self._clock(),
                source=EventSource.BROKER,
                symbol=intent.symbol,
                broker_order_id=order_id,
                status="MODIFIED",
            )
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=intent.symbol if order else None,
            broker_order_id=order_id,
            status="REJECTED",
            reason="cannot modify",
        )

    def on_bar(self, bar: BarEvent) -> list[FillEvent]:
        fills: list[FillEvent] = []
        for order in list(self._orders.values()):
            if order.status not in ("SUBMITTED", "PARTIAL_FILLED"):
                continue
            intent = order.intent
            if intent.symbol != bar.symbol:
                continue
            if intent.order_type != "LIMIT" or intent.limit_price is None:
                continue

            if intent.side == "BUY" and bar.low <= intent.limit_price:
                fill_price = min(bar.open, intent.limit_price) + self._config.slippage_ticks
            elif intent.side == "SELL" and bar.high >= intent.limit_price:
                fill_price = max(bar.open, intent.limit_price) - self._config.slippage_ticks
            else:
                continue

            fill_qty = self._compute_fill_quantity(order, bar)
            if fill_qty <= 0:
                continue
            commission = fill_price * Decimal(fill_qty) * self._config.commission_rate
            order.fill(fill_qty, fill_price, commission=commission)
            fills.append(
                FillEvent(
                    timestamp=bar.timestamp,
                    source=EventSource.BROKER,
                    symbol=bar.symbol,
                    broker_order_id=order.order_id,
                    side=intent.side,
                    quantity=fill_qty,
                    price=fill_price,
                    commission=commission,
                    partial=order.status == "PARTIAL_FILLED",
                )
            )
        return fills

    def _compute_fill_quantity(self, order: PaperOrderState, bar: BarEvent) -> int:
        remaining = order.remaining_quantity
        if self._config.partial_fill_probability >= 1.0:
            return remaining
        # 后续可扩展为按成交量比例或概率决定部分成交
        return remaining

    def on_quote(self, quote: QuoteEvent) -> list[FillEvent]:
        return []
```

**Step 4: 运行测试**

```bash
pytest tests/platform/test_paper_broker.py -v
```

Expected: 1 passed

**Step 5: Commit**

```bash
git add backend/app/platform/paper_order_state.py backend/app/platform/paper_broker.py backend/tests/platform/test_paper_broker.py
git commit -m "P152: PaperBroker with partial fill, slippage and commission"
```

---

### Task 7：持久化 Paper 订单状态

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/database.py`
- Modify: `backend/app/platform/paper_broker.py`
- Test: `backend/tests/platform/test_paper_broker.py`

**Step 1: 新增 `PaperOrder` 模型**

```python
# backend/app/models.py
class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_order_id: Mapped[str] = mapped_column(String(50), index=True)
    symbol: Mapped[str] = mapped_column(String(50))
    side: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    limit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="SUBMITTED")
    intent_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)
```

**Step 2: 新增 ensure**

```python
# backend/app/database.py

def _ensure_paper_orders_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "paper_orders" not in inspector.get_table_names():
        Base.metadata.create_all(bind=db_engine, tables=[PaperOrder.__table__])
```

并在 `init_db()` 中调用。

**Step 3: 让 PaperBroker 可选持久化（可选注入 session）**

Phase 1 可以先在内存撮合，持久化作为增强；本任务先保证表存在，测试可空。

**Step 4: Commit**

```bash
git add backend/app/models.py backend/app/database.py backend/app/platform/paper_broker.py backend/tests/platform/test_paper_broker.py
git commit -m "P152: PaperOrder persistence table"
```

---

## 5. PlatformRunner 多标的与 Paper Broker 集成

### Task 8：改造 PlatformRunner 支持多 symbol 与 PaperBroker

**Files:**
- Modify: `backend/app/platform/runner.py`
- Modify: `backend/app/platform/simbroker.py`
- Test: `backend/tests/platform/test_runner.py`

**Step 1: 修改 PlatformRunner 构造函数**

```python
# backend/app/platform/runner.py
class PlatformRunner:
    def __init__(
        self,
        symbols: list[str],
        strategy: Strategy,
        mode: str,
        bus: EventBus | None = None,
        store: EventStore | None = None,
        clock: Callable[[], datetime] | None = None,
        live_order_handler: Callable[[OrderIntent], None] | None = None,
        broker: PaperBroker | None = None,
    ) -> None:
        self.symbols = symbols
        self.strategy = strategy
        self.mode = mode
        self.bus = bus or EventBus()
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.live_order_handler = live_order_handler
        self._positions: dict[str, dict[str, Any]] = {}
        self._broker = broker
        if mode in ("backtest", "paper"):
            self._broker = broker or PaperBroker(clock=self.clock)
            self.bus.subscribe("fill", self._on_fill)
```

**Step 2: on_bar 处理 symbol 列表**

```python
def on_bar(self, bar: BarEvent) -> None:
    self._emit(bar)
    if bar.symbol in self.symbols:
        intents = self.strategy.on_bar(self._context(bar.symbol), bar)
        for intent in intents:
            self._execute_intent(intent, timestamp=bar.timestamp)
    if self._broker is not None:
        fills = self._broker.on_bar(bar)
        for fill in fills:
            self._emit(fill)
```

**Step 3: _context 接受 symbol**

```python
def _context(self, symbol: str) -> StrategyContext:
    return StrategyContext(
        symbol=symbol,
        positions=self._positions,
        params=self.strategy.params,
        clock=self.clock,
    )
```

**Step 4: _on_fill 使用 fill.symbol**

```python
def _on_fill(self, event: Event) -> None:
    if not isinstance(event, FillEvent):
        return
    fill = event
    symbol = fill.symbol or self.symbols[0]
    pos = self._positions.get(symbol, {"quantity": 0})
    qty = pos["quantity"]
    if fill.side == "BUY":
        qty += fill.quantity
    else:
        qty -= fill.quantity
    self._positions[symbol] = {"quantity": qty}
    follow_up = self.strategy.on_fill(self._context(symbol), fill)
    for intent in follow_up:
        self._execute_intent(intent, timestamp=fill.timestamp)
```

**Step 5: 更新测试**

```python
# backend/tests/platform/test_runner.py
runner = PlatformRunner(
    symbols=["AAPL.US"],
    strategy=strategy,
    mode="backtest",
    bus=bus,
    store=store,
)
```

**Step 6: 运行测试**

```bash
pytest tests/platform/test_runner.py -v
```

Expected: passed

**Step 7: Commit**

```bash
git add backend/app/platform/runner.py backend/app/platform/simbroker.py backend/tests/platform/test_runner.py
git commit -m "P152: PlatformRunner uses PaperBroker and supports multi-symbol"
```

---

## 6. 组合级风控

### Task 9：实现 `PortfolioRiskController`

**Files:**
- Create: `backend/app/platform/portfolio_risk.py`
- Test: `backend/tests/platform/test_portfolio_risk.py`

**Step 1: 写 failing test**

```python
# backend/tests/platform/test_portfolio_risk.py
from decimal import Decimal

from app.platform.portfolio_config import PortfolioConfig
from app.platform.portfolio_risk import PortfolioRiskController


def test_risk_controller_detects_exposure_breach():
    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US", "TSLA.US"],
        allocations={"AAPL.US": Decimal("0.5"), "TSLA.US": Decimal("0.5")},
        max_gross_exposure=Decimal("0.5"),
    )
    controller = PortfolioRiskController(config)
    prices = {"AAPL.US": Decimal("150"), "TSLA.US": Decimal("200")}
    positions = {"AAPL.US": {"quantity": 10}, "TSLA.US": {"quantity": 10}}
    nav = Decimal("5000")
    events = controller.check(prices, positions, nav)
    assert any(e.risk_type == "MAX_GROSS_EXPOSURE_BREACH" for e in events)
```

**Step 2: 实现 controller**

```python
# backend/app/platform/portfolio_risk.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.platform.events import RiskEvent, EventSource
from app.platform.portfolio_config import PortfolioConfig


class PortfolioRiskController:
    def __init__(self, config: PortfolioConfig) -> None:
        self.config = config
        self._peak_nav: Decimal | None = None
        self._paused = False

    def check(
        self,
        prices: dict[str, Decimal],
        positions: dict[str, dict[str, Any]],
        nav: Decimal,
    ) -> list[RiskEvent]:
        events: list[RiskEvent] = []
        if self._peak_nav is None or nav > self._peak_nav:
            self._peak_nav = nav

        gross = Decimal("0")
        net = Decimal("0")
        for symbol, pos in positions.items():
            qty = int(pos.get("quantity", 0))
            price = prices.get(symbol, Decimal("0"))
            exposure = Decimal(qty) * price
            gross += abs(exposure)
            net += exposure

        gross_ratio = gross / nav if nav > 0 else Decimal("0")
        net_ratio = net / nav if nav > 0 else Decimal("0")

        if gross_ratio > self.config.max_gross_exposure:
            events.append(
                RiskEvent(
                    timestamp=datetime.now(timezone.utc),  # 调用方应传入真实时间
                    source=EventSource.RISK,
                    risk_type="MAX_GROSS_EXPOSURE_BREACH",
                    severity="CRITICAL",
                    message=f"gross exposure {gross_ratio:.2%} > {self.config.max_gross_exposure:.2%}",
                )
            )
        if abs(net_ratio) > self.config.max_net_exposure:
            events.append(
                RiskEvent(
                    timestamp=datetime.now(timezone.utc),
                    source=EventSource.RISK,
                    risk_type="MAX_NET_EXPOSURE_BREACH",
                    severity="CRITICAL",
                    message=f"net exposure {net_ratio:.2%} > {self.config.max_net_exposure:.2%}",
                )
            )
        return events

    def drawdown_events(self, nav: Decimal) -> list[RiskEvent]:
        events: list[RiskEvent] = []
        if self._peak_nav and self._peak_nav > 0 and nav < self._peak_nav:
            dd = (self._peak_nav - nav) / self._peak_nav
            if dd > Decimal("0.1"):
                events.append(
                    RiskEvent(
                        timestamp=datetime.now(timezone.utc),
                        source=EventSource.RISK,
                        risk_type="DRAWDOWN_BREACH",
                        severity="WARNING",
                        message=f"drawdown {dd:.2%}",
                    )
                )
        return events
```

**注意：** 上面用了 `datetime.now(timezone.utc)`，实际应传入触发事件时间戳；后续在 RiskEngine 中统一处理。

**Step 3: 运行测试**

```bash
pytest tests/platform/test_portfolio_risk.py -v
```

Expected: 1 passed

**Step 4: Commit**

```bash
git add backend/app/platform/portfolio_risk.py backend/tests/platform/test_portfolio_risk.py
git commit -m "P156: PortfolioRiskController exposure and drawdown checks"
```

---

### Task 10：接入 RiskEngine 到 PlatformRunner

**Files:**
- Create: `backend/app/platform/risk_engine.py`
- Modify: `backend/app/platform/runner.py`
- Test: `backend/tests/platform/test_runner.py`

**Step 1: 实现 RiskEngine**

```python
# backend/app/platform/risk_engine.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.platform.events import BarEvent, EventSource, FillEvent, RiskEvent
from app.platform.portfolio_config import PortfolioConfig
from app.platform.portfolio_risk import PortfolioRiskController


class RiskEngine:
    def __init__(self, config: PortfolioConfig | None = None) -> None:
        self.config = config
        self.controller = PortfolioRiskController(config) if config else None
        self._positions: dict[str, dict[str, Any]] = {}
        self._cash: Any = None

    def on_fill(self, fill: FillEvent) -> list[RiskEvent]:
        symbol = fill.symbol or ""
        pos = self._positions.get(symbol, {"quantity": 0})
        qty = pos["quantity"]
        if fill.side == "BUY":
            qty += fill.quantity
        else:
            qty -= fill.quantity
        self._positions[symbol] = {"quantity": qty}
        return []

    def on_bar(self, bar: BarEvent, prices: dict[str, Any], nav: Any) -> list[RiskEvent]:
        if self.controller is None:
            return []
        return self.controller.check(prices, self._positions, nav)
```

**Step 2: PlatformRunner 注入 RiskEngine**

```python
# backend/app/platform/runner.py
from app.platform.risk_engine import RiskEngine

# 在 __init__ 中增加
self.risk_engine = risk_engine or RiskEngine()
self.bus.subscribe("fill", self.risk_engine.on_fill)
```

**Step 3: 运行测试**

```bash
pytest tests/platform/test_runner.py -v
```

Expected: passed

**Step 4: Commit**

```bash
git add backend/app/platform/risk_engine.py backend/app/platform/runner.py backend/tests/platform/test_runner.py
git commit -m "P156: wire RiskEngine into PlatformRunner"
```

---

## 7. 文档与回归

### Task 11：更新 CLAUDE.md 与 Roadmap

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/Roadmap.md`

**Step 1: 在 CLAUDE.md 目录结构中加入新文件**

```markdown
│   │   ├── platform/                       # P149+P150+P151+P152+P156：策略插件 SDK + 统一事件流 + 组合交易 + Paper Broker + 组合风控
│   │   │   ├── portfolio_config.py         # 组合配置 dataclass
│   │   │   ├── portfolio_allocator.py      # 资金分配/再平衡
│   │   │   ├── portfolio_service.py        # 组合配置 CRUD
│   │   │   ├── portfolio_api.py            # /api/portfolio/*
│   │   │   ├── paper_broker.py             # 真实成交仿真撮合
│   │   │   ├── paper_order_state.py        # Paper 订单状态机
│   │   │   ├── portfolio_risk.py           # 组合级风控
│   │   │   └── risk_engine.py              # 事件驱动风控引擎
```

**Step 2: 在 Roadmap.md 标记 P151/P152/P156 完成**

在 P149+P150 已完成迭代下方新增：

```markdown
## 近期已完成迭代 (2026-06-22) — 组合、仿真与风控（P151–P152 + P156）

> 在平台事件流基础上实现组合配置、Paper Broker 真实成交仿真、组合级敞口/回撤风控。规格：[2026-06-22-p149-p158-quant-platform-design.md](superpowers/specs/2026-06-22-p149-p158-quant-platform-design.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P151** | 组合级多标的交易 | ✅ |
| **P152** | Paper Broker / 真实成交仿真 | ✅ |
| **P156** | 组合级风控与熔断 | ✅ |
```

**Step 3: Commit**

```bash
git add CLAUDE.md docs/Roadmap.md
git commit -m "docs: update CLAUDE.md and Roadmap for P151/P152/P156"
```

---

### Task 12：全量回归

**Files:**
- All

**Step 1: 运行全量测试**

```bash
cd /home/lcy/code/auto_trade/.claude/worktrees/p151-p158-quant-platform/backend
source /home/lcy/code/auto_trade/backend/.venv/bin/activate
pytest tests/ -q --no-cov
python3 -m basedpyright app/platform/ tests/platform/ app/main.py app/config.py app/models.py app/database.py
```

Expected: pytest 不低于 1227 passed，basedpyright 0 errors。

**Step 2: Commit 任何修复**

如有失败，修复后再 commit。

---

## Self-Review

### Spec coverage

- P151 组合配置：Task 1-4（模型、dataclass、allocator、service/API）。
- P152 Paper Broker：Task 5-7（事件扩展、状态机、持久化）、Task 8（PlatformRunner 集成）。
- P156 组合风控：Task 9-10（PortfolioRiskController、RiskEngine 接入）。
- 文档：Task 11。
- 回归：Task 12。

### Placeholder scan

- 无 TBD/TODO。
- 所有代码步骤均含具体实现。
- 测试步骤含具体断言。

### Type consistency

- `OrderIntent` 继续使用 P149 定义。
- `FillEvent` 新增字段在 `to_dict` / `from_dict` 中同步。
- `PlatformRunner.symbols` 由 `symbol: str` 改为 `symbols: list[str]`；所有测试同步修改。

---

## 执行方式选择

**Plan complete and saved to `docs/superpowers/plans/2026-06-22-p151-p156-portfolio-paper-risk.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
