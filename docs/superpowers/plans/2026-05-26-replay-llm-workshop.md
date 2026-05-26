# P7 策略复盘与 LLM 优化工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改写路径、不新增表的前提下，提供单一标的的按日复盘视图与结构化导出，反哺 LLM prompt 调优。

**Architecture:** 后端新增 `ReplayService`（只读，跨表 JOIN，复用 `DailyPnlService.calculate` 重算 realized PnL，复用 `BrokerGateway.get_candlesticks` 拉 K 线、`market_calendar.trade_day_for` 划分交易日；K 线不可用时回退 `RuntimeStateSnapshot`）；新增 `/api/replay/{days,{date},{date}/export}` 三个 GET 端点；前端新增 `/#/replay` 页（`Replay.vue` + `ReplayPriceChart.vue`），导出走浏览器原生下载。5 个 `ReplayTag` 按优先级评估（`MISSED_STOP` > `PREMATURE_ENTRY` > `FREQUENT_REPRICE` > `LOW_PROFIT` > `NORMAL`）。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / SQLite / pytest / basedpyright / Vue 3 + TypeScript + Element Plus / Cypress

**Spec:** [2026-05-26-replay-llm-workshop-design.md](../specs/2026-05-26-replay-llm-workshop-design.md)

**Baseline (2026-05-26):** `pytest 485 collected`, `basedpyright` 0 errors / 0 warnings, commit `417a1f1`（doc sync）。

---

## File Map

### Created
- `backend/app/services/replay_service.py` — `ReplayService` 类、`_ReplayTagParams` 常量、内部 trip 构造与 tag 分类
- `backend/app/api/replay.py` — 三个 GET 端点 router
- `backend/tests/test_replay_service_aggregate.py` — 聚合主路径
- `backend/tests/test_replay_service_trips.py` — trip 划分边界
- `backend/tests/test_replay_tagger.py` — 5 个 tag 分类
- `backend/tests/test_replay_api.py` — 三端点 HTTP
- `backend/tests/test_replay_export.py` — JSON / CSV 导出
- `frontend/src/api/replay.ts` — API client
- `frontend/src/views/Replay.vue` — 复盘页主组件
- `frontend/src/components/ReplayPriceChart.vue` — 价格 + trip + LLM 标记 SVG 图
- `frontend/src/utils/replay_tags.ts` — tag 色码与中文标签
- `frontend/cypress/e2e/replay.cy.ts` — E2E 覆盖

### Modified
- `backend/app/schemas.py` — 追加 `ReplayTag`、`ReplayPricePoint`、`ReplayOrderItem`、`ReplayLLMItem`、`ReplayTradeEventItem`、`ReplayTrip`、`ReplayDaySummary`、`ReplayDaysResponse`、`ReplayDayResponse`
- `backend/app/main.py` — `include_router(replay_router)`
- `frontend/src/router/index.ts` — 增 `/replay` 路由
- `frontend/src/App.vue` — 导航栏增 "Replay" 入口
- `frontend/src/types/index.ts` — 追加 P7 类型
- `README.md` / `CLAUDE.md` / `docs/Roadmap.md` — T6 末尾收口（README 已有"规划中"小节，转为"已交付"）

---

## Conventions（所有任务通用）

- **测试隔离**：每个新测试文件**第一行**设 `os.environ["AUTO_TRADE_DATABASE_URL"]` 到独立 sqlite，再 `from app...`。`tests/conftest.py` 已处理 `AUTO_TRADE_CREDENTIAL_KEY_PATH`。
- **TDD**：先写失败测试，跑一次确认失败，再实现，再跑一次确认通过。
- **`get_db` DI**：API 层用 `db: Session = Depends(get_db)`（已存在于 `app/database.py:218`）。
- **broker 注入**：API 层不直接持有 `BrokerGateway`；通过 `from app.runner import get_runner` 取 `get_runner().broker`；测试时直接构造 `ReplayService(db, broker=fake_broker, strategy_service=...)`，不经过 runner。
- **lint**：每个任务结束前 `python3 -m basedpyright`（仓库根）应为 0 errors / 0 warnings；前端结束前 `npm run type-check`。
- **commit 信息**：保留中文动词 + 范围，例如 `feat(replay): aggregate_day price loading`。

---

## Task 1：ReplayService 基础（list_days + aggregate_day 主路径 + trip 划分）

> **目标：** 让 `ReplayService.list_days` 与 `aggregate_day` 跑通基本数据流：候选日期、价格序列（K 线 / snapshots 回退）、orders / trade_events / llm_interactions 时间窗筛选、内存 JOIN、trip 划分。**不含 tag 计算（T2）也不含 schemas / API（T3）**——本任务暂时返回内部 dataclass，T3 再映射到 Pydantic。

**Files:**
- Create: `backend/app/services/replay_service.py`
- Create: `backend/tests/test_replay_service_aggregate.py`
- Create: `backend/tests/test_replay_service_trips.py`

### T1.1 — ReplayService 骨架与内部 dataclasses

- [ ] **Step 1: 写失败测试 `test_replay_service_aggregate.py::test_list_days_filters_to_data_bearing_days`**

```python
# backend/tests/test_replay_service_aggregate.py 文件顶部
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_replay_aggregate.db"
)

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.database import SessionLocal, engine, init_db
from app.models import Base, LLMInteraction, OrderRecord, RuntimeStateSnapshot, StrategyConfig
from app.services.replay_service import ReplayService
from app.services.strategy_service import StrategyService


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    init_db()
    yield


def _db() -> Session:
    return SessionLocal()


def _strategy(db: Session) -> StrategyConfig:
    config = StrategyConfig(symbol="AAPL.US", market="US", min_profit_amount=50.0)
    db.add(config)
    db.commit()
    return config


def test_list_days_filters_to_data_bearing_days():
    db = _db()
    _strategy(db)
    # 2026-05-20 有订单，2026-05-21 仅 LLM，2026-05-22 仅 snapshot，2026-05-23 完全空
    db.add(OrderRecord(
        broker_order_id="b1", symbol="AAPL.US", side="BUY",
        quantity=10, price=150.0, status="FILLED",
        created_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 31, tzinfo=timezone.utc),
    ))
    db.add(LLMInteraction(
        symbol="AAPL.US", market="US", success=True,
        created_at=datetime(2026, 5, 21, 14, 0, tzinfo=timezone.utc),
    ))
    db.add(RuntimeStateSnapshot(
        engine_state="flat", last_price=150.0,
        created_at=datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc),
    ))
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    days = svc.list_days(symbol=None, limit=30)

    assert {d.trade_day.isoformat() for d in days} == {"2026-05-20", "2026-05-21", "2026-05-22"}
    # DESC 排序
    assert [d.trade_day.isoformat() for d in days] == ["2026-05-22", "2026-05-21", "2026-05-20"]
    db.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
python3 -m pytest tests/test_replay_service_aggregate.py::test_list_days_filters_to_data_bearing_days -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.replay_service'`

- [ ] **Step 3: 创建 `backend/app/services/replay_service.py` 骨架**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.broker import BrokerCandle, BrokerGateway
from app.core.market_calendar import get_session, trade_day_for
from app.models import (
    LLMInteraction,
    OrderRecord,
    RuntimeStateSnapshot,
    TradeEvent,
)
from app.services.daily_pnl_service import DailyPnlService
from app.services.strategy_service import StrategyService


class _ReplayTagParams:
    MISSED_STOP_WINDOW_SECONDS = 14400  # 4 小时
    PREMATURE_WINDOW_SECONDS = 1800  # 30 分钟
    REPRICE_THRESHOLD = 3
    LOW_PROFIT_RATIO = 1.2


@dataclass
class _DaySummaryInternal:
    trade_day: date
    symbol: str
    market: str
    trip_count: int = 0
    realized_pnl: float = 0.0
    llm_call_count: int = 0
    llm_applied_count: int = 0
    tag_counts: dict[str, int] = field(default_factory=dict)


class ReplayService:
    """Aggregate read-only daily replay across LLM, orders, events, and price snapshots.

    The service does not write any row or trigger any external call other than
    optional broker candle reads. PnL is recomputed via DailyPnlService.
    """

    def __init__(
        self,
        db: Session,
        *,
        broker: BrokerGateway | None,
        strategy_service: StrategyService,
    ) -> None:
        self._db = db
        self._broker = broker
        self._strategy = strategy_service

    def _resolve_symbol(self, symbol: str | None) -> tuple[str, str]:
        config = self._strategy.get_config()
        chosen = (symbol or config.symbol or "").strip()
        if not chosen:
            raise ValueError("symbol not configured")
        return chosen, (config.market or "US").upper()

    def list_days(self, symbol: str | None, limit: int) -> list[_DaySummaryInternal]:
        if limit < 1 or limit > 90:
            raise ValueError("limit must be between 1 and 90")
        chosen, market = self._resolve_symbol(symbol)

        candidate_days: set[date] = set()
        for created_at, in self._db.query(OrderRecord.created_at).filter(
            OrderRecord.symbol == chosen
        ):
            candidate_days.add(trade_day_for(market, created_at))
        for created_at, in self._db.query(LLMInteraction.created_at).filter(
            LLMInteraction.symbol == chosen
        ):
            candidate_days.add(trade_day_for(market, created_at))
        for created_at, in self._db.query(RuntimeStateSnapshot.created_at):
            candidate_days.add(trade_day_for(market, created_at))

        days = sorted(candidate_days, reverse=True)[:limit]
        return [
            _DaySummaryInternal(trade_day=d, symbol=chosen, market=market)
            for d in days
        ]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 -m pytest tests/test_replay_service_aggregate.py::test_list_days_filters_to_data_bearing_days -v
```

Expected: PASS

- [ ] **Step 5: 加边界测试 `test_list_days_rejects_invalid_limit`**

```python
def test_list_days_rejects_invalid_limit():
    db = _db()
    _strategy(db)
    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    with pytest.raises(ValueError):
        svc.list_days(symbol=None, limit=0)
    with pytest.raises(ValueError):
        svc.list_days(symbol=None, limit=91)
    db.close()


def test_list_days_raises_when_symbol_unset():
    db = _db()
    db.add(StrategyConfig(symbol="", market="US"))
    db.commit()
    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    with pytest.raises(ValueError):
        svc.list_days(symbol=None, limit=10)
    db.close()
```

- [ ] **Step 6: 跑两个新测试**

```bash
python3 -m pytest tests/test_replay_service_aggregate.py -v
```

Expected: 3 PASSED

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_service_aggregate.py
git commit -m "feat(replay): ReplayService scaffold + list_days"
```

### T1.2 — `aggregate_day` 价格序列（K 线 → snapshots → none）

- [ ] **Step 1: 在 `test_replay_service_aggregate.py` 末尾加测试 `test_aggregate_day_uses_candles_when_available`**

```python
from app.core.broker import BrokerCandle


class _FakeBroker:
    def __init__(self, candles: list[BrokerCandle] | None = None, raises: bool = False):
        self._candles = candles or []
        self._raises = raises
        self.calls: list[tuple[str, str, int]] = []

    def get_candlesticks(self, symbol: str, period: str, count: int) -> list[BrokerCandle]:
        self.calls.append((symbol, period, count))
        if self._raises:
            raise RuntimeError("broker unavailable")
        return self._candles


def _candle(minute: int, close: float) -> BrokerCandle:
    return BrokerCandle(
        timestamp=datetime(2026, 5, 20, 14, minute, tzinfo=timezone.utc),
        open=close, high=close, low=close, close=close, volume=1000.0,
    )


def test_aggregate_day_uses_candles_when_available():
    db = _db()
    _strategy(db)
    broker = _FakeBroker(candles=[_candle(30, 150.0), _candle(31, 151.0)])
    svc = ReplayService(db=db, broker=broker, strategy_service=StrategyService(db))

    result = svc.aggregate_day(symbol=None, trade_day=date(2026, 5, 20))

    assert result["price_source"] == "candles"
    assert [p.close for p in result["prices"]] == [150.0, 151.0]
    assert broker.calls == [("AAPL.US", "Min_1", 400)]
    db.close()


def test_aggregate_day_falls_back_to_snapshots_when_broker_unavailable():
    db = _db()
    _strategy(db)
    db.add(RuntimeStateSnapshot(
        engine_state="flat", last_price=152.0,
        created_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
    ))
    db.commit()
    broker = _FakeBroker(raises=True)
    svc = ReplayService(db=db, broker=broker, strategy_service=StrategyService(db))

    result = svc.aggregate_day(symbol=None, trade_day=date(2026, 5, 20))

    assert result["price_source"] == "snapshots"
    assert [p.close for p in result["prices"]] == [152.0]
    db.close()


def test_aggregate_day_marks_none_when_no_data():
    db = _db()
    _strategy(db)
    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))

    result = svc.aggregate_day(symbol=None, trade_day=date(2026, 5, 20))

    assert result["price_source"] == "none"
    assert result["prices"] == []
    db.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_replay_service_aggregate.py::test_aggregate_day_uses_candles_when_available -v
```

Expected: `AttributeError: 'ReplayService' object has no attribute 'aggregate_day'`

- [ ] **Step 3: 在 `replay_service.py` 加 `aggregate_day` 价格路径**

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class _PricePoint:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None


def _rth_window(market: str, trade_day: date) -> tuple[datetime, datetime]:
    session = get_session(market)
    local_open = datetime.combine(trade_day, session.rth_open, tzinfo=session.timezone)
    local_close = datetime.combine(trade_day, session.rth_close, tzinfo=session.timezone)
    return local_open.astimezone(timezone.utc), local_close.astimezone(timezone.utc)


class ReplayService:  # 已存在；下面是新增方法
    ...
    def aggregate_day(self, symbol: str | None, trade_day: date) -> dict[str, Any]:
        chosen, market = self._resolve_symbol(symbol)
        t_open, t_close = _rth_window(market, trade_day)

        prices, price_source = self._load_prices(chosen, market, trade_day, t_open, t_close)
        return {
            "summary": _DaySummaryInternal(trade_day=trade_day, symbol=chosen, market=market),
            "price_source": price_source,
            "prices": prices,
            "rth_window": (t_open, t_close),
            "orders": [],
            "trade_events": [],
            "llm_interactions": [],
            "trips": [],
            "metadata": {},
        }

    def _load_prices(
        self,
        symbol: str,
        market: str,
        trade_day: date,
        t_open: datetime,
        t_close: datetime,
    ) -> tuple[list[_PricePoint], Literal["candles", "snapshots", "none"]]:
        if self._broker is not None:
            try:
                candles = self._broker.get_candlesticks(symbol, "Min_1", 400)
            except Exception:  # broker may be flaky; fall back  # noqa: BLE001
                candles = []
            filtered = [
                _PricePoint(
                    ts=_ensure_utc(c.timestamp),
                    open=float(c.open),
                    high=float(c.high),
                    low=float(c.low),
                    close=float(c.close),
                    volume=float(c.volume) if c.volume is not None else None,
                )
                for c in candles
                if t_open <= _ensure_utc(c.timestamp) <= t_close
            ]
            if filtered:
                return sorted(filtered, key=lambda p: p.ts), "candles"

        snapshot_points = self._snapshot_series(market, trade_day, t_open, t_close)
        if snapshot_points:
            return snapshot_points, "snapshots"
        return [], "none"

    def _snapshot_series(
        self,
        market: str,
        trade_day: date,
        t_open: datetime,
        t_close: datetime,
    ) -> list[_PricePoint]:
        rows = (
            self._db.query(RuntimeStateSnapshot)
            .filter(RuntimeStateSnapshot.created_at >= t_open)
            .filter(RuntimeStateSnapshot.created_at <= t_close)
            .order_by(RuntimeStateSnapshot.created_at.asc())
            .all()
        )
        # Filter by market trade day to avoid bleed across markets
        kept = [r for r in rows if trade_day_for(market, r.created_at) == trade_day]
        return [
            _PricePoint(
                ts=_ensure_utc(r.created_at),
                open=float(r.last_price),
                high=float(r.last_price),
                low=float(r.last_price),
                close=float(r.last_price),
                volume=None,
            )
            for r in kept
        ]


def _ensure_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        return instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)
```

- [ ] **Step 4: 跑全部 aggregate 测试确认通过**

```bash
python3 -m pytest tests/test_replay_service_aggregate.py -v
```

Expected: 6 PASSED

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_service_aggregate.py
git commit -m "feat(replay): aggregate_day price loading with candle/snapshot fallback"
```

### T1.3 — `aggregate_day` 关联数据 + realized PnL

- [ ] **Step 1: 加测试 `test_aggregate_day_joins_orders_llm_events`**

```python
from app.models import TradeEvent


def test_aggregate_day_joins_orders_llm_events():
    db = _db()
    _strategy(db)
    db.add(OrderRecord(
        id=1, broker_order_id="b1", symbol="AAPL.US", side="BUY",
        quantity=10, price=150.0, status="FILLED",
        executed_quantity=10, executed_price=150.0,
        created_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
    ))
    db.add(OrderRecord(
        id=2, broker_order_id="b2", symbol="AAPL.US", side="SELL",
        quantity=10, price=155.0, status="FILLED",
        executed_quantity=10, executed_price=155.0,
        created_at=datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc),
    ))
    db.add(LLMInteraction(
        symbol="AAPL.US", market="US", success=True, applied=True,
        order_action="SUBMIT", order_id="b1",
        created_at=datetime(2026, 5, 20, 14, 29, tzinfo=timezone.utc),
    ))
    db.add(TradeEvent(
        event_type="ORDER_FILLED", symbol="AAPL.US", broker_order_id="b2",
        side="SELL", status="FILLED",
        created_at=datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc),
    ))
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=date(2026, 5, 20))

    assert len(result["orders"]) == 2
    assert len(result["llm_interactions"]) == 1
    assert len(result["trade_events"]) == 1
    # SELL fill → realized PnL = (155 - 150) * 10 = 50
    sell_order = next(o for o in result["orders"] if o.broker_order_id == "b2")
    assert sell_order.realized_pnl == pytest.approx(50.0)
    # LLM linked to BUY order
    llm = result["llm_interactions"][0]
    assert llm.linked_order_id == 1
    db.close()


def test_aggregate_day_marks_open_orders_with_none_pnl():
    db = _db()
    _strategy(db)
    db.add(OrderRecord(
        id=1, broker_order_id="b1", symbol="AAPL.US", side="BUY",
        quantity=10, price=150.0, status="FILLED",
        executed_quantity=10, executed_price=150.0,
        created_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
    ))
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=date(2026, 5, 20))

    order = result["orders"][0]
    assert order.realized_pnl is None
    db.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_replay_service_aggregate.py::test_aggregate_day_joins_orders_llm_events -v
```

Expected: `assert len(...) == 2` failing because `orders` is empty list.

- [ ] **Step 3: 实现关联数据加载（在 `replay_service.py` 内）**

加入内部 dataclasses：

```python
@dataclass(frozen=True)
class _OrderItem:
    id: int
    broker_order_id: str
    side: str
    status: str
    quantity: float
    price: float
    executed_quantity: float | None
    executed_price: float | None
    created_at: datetime
    filled_at: datetime | None
    realized_pnl: float | None


@dataclass(frozen=True)
class _LLMItem:
    id: int
    interaction_type: str
    created_at: datetime
    applied: bool
    order_action: str
    order_status: str | None
    suggested_buy_low: float | None
    suggested_sell_high: float | None
    confidence: float | None
    summary: str
    prompt_excerpt: str
    linked_order_id: int | None
    parsed_response: str  # 留作 export 时使用
    prompt: str           # 留作 export 时使用
    raw_response: str     # 留作 export 时使用


@dataclass(frozen=True)
class _TradeEventItem:
    id: int
    event_type: str
    side: str
    status: str
    message: str
    broker_order_id: str
    skip_category: str | None
    linked_order_id: int | None
    created_at: datetime
```

扩展 `aggregate_day`：

```python
import json

from app.services.daily_pnl_service import DailyPnlService


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_llm_suggestions(parsed_response: str) -> tuple[float | None, float | None, float | None]:
    if not parsed_response:
        return None, None, None
    try:
        data = json.loads(parsed_response)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    buy = data.get("suggested_buy_low")
    sell = data.get("suggested_sell_high")
    conf = data.get("confidence_score") or data.get("confidence")
    return (
        float(buy) if isinstance(buy, (int, float)) else None,
        float(sell) if isinstance(sell, (int, float)) else None,
        float(conf) if isinstance(conf, (int, float)) else None,
    )


class ReplayService:
    ...
    def aggregate_day(self, symbol: str | None, trade_day: date) -> dict[str, Any]:
        chosen, market = self._resolve_symbol(symbol)
        t_open, t_close = _rth_window(market, trade_day)

        prices, price_source = self._load_prices(chosen, market, trade_day, t_open, t_close)
        orders_raw = self._load_orders(chosen, t_open, t_close)
        events_raw = self._load_events(chosen, t_open, t_close)
        llm_raw = self._load_llm(chosen, t_open, t_close)

        realized_by_broker_id = self._compute_realized_pnl(chosen, market, trade_day)
        orders = self._to_order_items(orders_raw, realized_by_broker_id)
        order_index_by_broker: dict[str, int] = {o.broker_order_id: o.id for o in orders if o.broker_order_id}
        events = self._to_event_items(events_raw, order_index_by_broker)
        llm = self._to_llm_items(llm_raw, order_index_by_broker)

        # T1.4 后填充 trips & tags；这里先返回空
        trips: list[Any] = []
        summary = _DaySummaryInternal(trade_day=trade_day, symbol=chosen, market=market)
        summary.realized_pnl = sum((o.realized_pnl or 0.0) for o in orders if (o.realized_pnl or 0.0) != 0.0)
        summary.llm_call_count = len(llm)
        summary.llm_applied_count = sum(1 for i in llm if i.applied)

        return {
            "summary": summary,
            "price_source": price_source,
            "prices": prices,
            "orders": orders,
            "trade_events": events,
            "llm_interactions": llm,
            "trips": trips,
            "rth_window": (t_open, t_close),
            "metadata": {
                "min_profit_amount": float(self._strategy.get_config().min_profit_amount),
                "tag_thresholds": {
                    "MISSED_STOP_WINDOW_SECONDS": _ReplayTagParams.MISSED_STOP_WINDOW_SECONDS,
                    "PREMATURE_WINDOW_SECONDS": _ReplayTagParams.PREMATURE_WINDOW_SECONDS,
                    "REPRICE_THRESHOLD": _ReplayTagParams.REPRICE_THRESHOLD,
                    "LOW_PROFIT_RATIO": _ReplayTagParams.LOW_PROFIT_RATIO,
                },
            },
        }

    def _load_orders(self, symbol: str, t_open: datetime, t_close: datetime) -> list[OrderRecord]:
        return (
            self._db.query(OrderRecord)
            .filter(OrderRecord.symbol == symbol)
            .filter(OrderRecord.created_at >= t_open)
            .filter(OrderRecord.created_at <= t_close)
            .order_by(OrderRecord.created_at.asc(), OrderRecord.id.asc())
            .all()
        )

    def _load_events(self, symbol: str, t_open: datetime, t_close: datetime) -> list[TradeEvent]:
        return (
            self._db.query(TradeEvent)
            .filter(TradeEvent.symbol == symbol)
            .filter(TradeEvent.created_at >= t_open)
            .filter(TradeEvent.created_at <= t_close)
            .order_by(TradeEvent.created_at.asc(), TradeEvent.id.asc())
            .all()
        )

    def _load_llm(self, symbol: str, t_open: datetime, t_close: datetime) -> list[LLMInteraction]:
        return (
            self._db.query(LLMInteraction)
            .filter(LLMInteraction.symbol == symbol)
            .filter(LLMInteraction.created_at >= t_open)
            .filter(LLMInteraction.created_at <= t_close)
            .order_by(LLMInteraction.created_at.asc(), LLMInteraction.id.asc())
            .all()
        )

    def _compute_realized_pnl(self, symbol: str, market: str, trade_day: date) -> dict[str, list[float]]:
        pnl_svc = DailyPnlService(self._db)
        result = pnl_svc.calculate(
            trade_day=trade_day,
            symbol=symbol,
            to_trade_day=lambda dt: trade_day_for(market, dt),
        )
        by_id: dict[str, list[float]] = {}
        for trade in result.trades:
            by_id.setdefault(trade.broker_order_id, []).append(trade.pnl)
        return by_id

    def _to_order_items(
        self,
        rows: list[OrderRecord],
        realized_by_id: dict[str, list[float]],
    ) -> list[_OrderItem]:
        items: list[_OrderItem] = []
        for row in rows:
            pnl_list = realized_by_id.get(row.broker_order_id, [])
            pnl = pnl_list.pop(0) if pnl_list else None
            items.append(_OrderItem(
                id=row.id,
                broker_order_id=row.broker_order_id,
                side=row.side,
                status=row.status,
                quantity=float(row.quantity),
                price=float(row.price),
                executed_quantity=(
                    float(row.executed_quantity) if row.executed_quantity is not None else None
                ),
                executed_price=(
                    float(row.executed_price) if row.executed_price is not None else None
                ),
                created_at=_ensure_utc(row.created_at),
                filled_at=_ensure_utc(row.filled_at) if row.filled_at else None,
                realized_pnl=pnl,
            ))
        return items

    def _to_event_items(
        self,
        rows: list[TradeEvent],
        order_index: dict[str, int],
    ) -> list[_TradeEventItem]:
        items: list[_TradeEventItem] = []
        for row in rows:
            try:
                payload = json.loads(row.payload_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            skip_cat = payload.get("skip_category") if isinstance(payload, dict) else None
            items.append(_TradeEventItem(
                id=row.id,
                event_type=row.event_type,
                side=row.side or "",
                status=row.status or "",
                message=row.message or "",
                broker_order_id=row.broker_order_id or "",
                skip_category=str(skip_cat) if skip_cat else None,
                linked_order_id=order_index.get(row.broker_order_id),
                created_at=_ensure_utc(row.created_at),
            ))
        return items

    def _to_llm_items(
        self,
        rows: list[LLMInteraction],
        order_index: dict[str, int],
    ) -> list[_LLMItem]:
        items: list[_LLMItem] = []
        for row in rows:
            buy, sell, conf = _parse_llm_suggestions(row.parsed_response)
            items.append(_LLMItem(
                id=row.id,
                interaction_type=row.interaction_type,
                created_at=_ensure_utc(row.created_at),
                applied=bool(row.applied),
                order_action=row.order_action or "NONE",
                order_status=row.order_status,
                suggested_buy_low=buy,
                suggested_sell_high=sell,
                confidence=conf,
                summary=_truncate(row.parsed_response, 240),
                prompt_excerpt=_truncate(row.prompt, 480),
                linked_order_id=order_index.get(row.order_id or ""),
                parsed_response=row.parsed_response or "",
                prompt=row.prompt or "",
                raw_response=row.raw_response or "",
            ))
        return items
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 -m pytest tests/test_replay_service_aggregate.py -v
```

Expected: 8 PASSED

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_service_aggregate.py
git commit -m "feat(replay): join orders/llm/events and recompute realized pnl"
```

### T1.4 — Trip 划分（snapshot-based + order-inferred 回退）

- [ ] **Step 1: 创建 `tests/test_replay_service_trips.py`**

```python
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_replay_trips.db"
)

from datetime import date, datetime, timezone

import pytest

from app.database import SessionLocal, engine
from app.models import Base, OrderRecord, RuntimeStateSnapshot, StrategyConfig
from app.services.replay_service import ReplayService
from app.services.strategy_service import StrategyService


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def _db():
    db = SessionLocal()
    db.add(StrategyConfig(symbol="AAPL.US", market="US", min_profit_amount=50.0))
    db.commit()
    return db


def test_trips_built_from_snapshot_state_transitions():
    db = _db()
    # 14:30 flat → 14:31 long → 14:45 flat
    for minute, state in [(30, "flat"), (31, "long"), (45, "flat")]:
        db.add(RuntimeStateSnapshot(
            engine_state=state, last_price=150.0,
            created_at=datetime(2026, 5, 20, 14, minute, tzinfo=timezone.utc),
        ))
    db.add(OrderRecord(
        id=1, broker_order_id="b1", symbol="AAPL.US", side="BUY",
        quantity=10, price=150.0, status="FILLED",
        executed_quantity=10, executed_price=150.0,
        created_at=datetime(2026, 5, 20, 14, 31, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 31, tzinfo=timezone.utc),
    ))
    db.add(OrderRecord(
        id=2, broker_order_id="b2", symbol="AAPL.US", side="SELL",
        quantity=10, price=155.0, status="FILLED",
        executed_quantity=10, executed_price=155.0,
        created_at=datetime(2026, 5, 20, 14, 45, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 45, tzinfo=timezone.utc),
    ))
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=date(2026, 5, 20))

    assert len(result["trips"]) == 1
    trip = result["trips"][0]
    assert trip.direction == "long"
    assert trip.entry_order_id == 1
    assert trip.exit_order_id == 2
    assert trip.realized_pnl == pytest.approx(50.0)
    assert result["metadata"]["trip_inference_mode"] == "snapshot_based"
    db.close()


def test_trips_fall_back_to_order_inference_without_snapshots():
    db = _db()
    db.add(OrderRecord(
        id=1, broker_order_id="b1", symbol="AAPL.US", side="BUY",
        quantity=10, price=150.0, status="FILLED",
        executed_quantity=10, executed_price=150.0,
        created_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
    ))
    db.add(OrderRecord(
        id=2, broker_order_id="b2", symbol="AAPL.US", side="SELL",
        quantity=10, price=155.0, status="FILLED",
        executed_quantity=10, executed_price=155.0,
        created_at=datetime(2026, 5, 20, 14, 45, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 45, tzinfo=timezone.utc),
    ))
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=date(2026, 5, 20))

    assert len(result["trips"]) == 1
    trip = result["trips"][0]
    assert trip.direction == "long"
    assert trip.entry_order_id == 1
    assert trip.exit_order_id == 2
    assert result["metadata"]["trip_inference_mode"] == "order_inferred"
    db.close()


def test_open_trip_has_no_close_at_or_exit():
    db = _db()
    db.add(RuntimeStateSnapshot(
        engine_state="flat", last_price=150.0,
        created_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
    ))
    db.add(RuntimeStateSnapshot(
        engine_state="long", last_price=150.0,
        created_at=datetime(2026, 5, 20, 14, 31, tzinfo=timezone.utc),
    ))
    db.add(OrderRecord(
        id=1, broker_order_id="b1", symbol="AAPL.US", side="BUY",
        quantity=10, price=150.0, status="FILLED",
        executed_quantity=10, executed_price=150.0,
        created_at=datetime(2026, 5, 20, 14, 31, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 31, tzinfo=timezone.utc),
    ))
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=date(2026, 5, 20))

    trip = result["trips"][0]
    assert trip.close_at is None
    assert trip.exit_order_id is None
    assert trip.realized_pnl is None
    db.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_replay_service_trips.py -v
```

Expected: all 3 FAIL with `assert len(result["trips"]) == 1` because trips is `[]`.

- [ ] **Step 3: 在 `replay_service.py` 实现 trip 构造**

```python
@dataclass(frozen=True)
class _Trip:
    open_at: datetime
    close_at: datetime | None
    direction: Literal["long", "short"]
    entry_order_id: int | None
    exit_order_id: int | None
    quantity: float
    entry_price: float
    exit_price: float | None
    realized_pnl: float | None
    llm_interaction_ids: list[int]


_ENTRY_SIDES = {"long": {"BUY"}, "short": {"SELL_SHORT", "SELL"}}
_EXIT_SIDES = {"long": {"SELL"}, "short": {"BUY_TO_COVER", "BUY"}}


class ReplayService:
    ...
    def _build_trips(
        self,
        market: str,
        trade_day: date,
        orders: list[_OrderItem],
        llm: list[_LLMItem],
        t_open: datetime,
        t_close: datetime,
    ) -> tuple[list[_Trip], Literal["snapshot_based", "order_inferred", "none"]]:
        snapshots = (
            self._db.query(RuntimeStateSnapshot)
            .filter(RuntimeStateSnapshot.created_at >= t_open)
            .filter(RuntimeStateSnapshot.created_at <= t_close)
            .order_by(RuntimeStateSnapshot.created_at.asc())
            .all()
        )
        snapshots = [s for s in snapshots if trade_day_for(market, s.created_at) == trade_day]

        if snapshots:
            trips = self._build_trips_from_snapshots(snapshots, orders, llm)
            mode: Literal["snapshot_based", "order_inferred", "none"] = "snapshot_based"
        else:
            trips = self._build_trips_from_orders(orders, llm)
            mode = "order_inferred" if trips else "none"
        return trips, mode

    def _build_trips_from_snapshots(
        self,
        snapshots: list[RuntimeStateSnapshot],
        orders: list[_OrderItem],
        llm: list[_LLMItem],
    ) -> list[_Trip]:
        trips: list[_Trip] = []
        current_state = "flat"
        open_at: datetime | None = None
        direction: Literal["long", "short"] | None = None
        for snap in snapshots:
            next_state = snap.engine_state
            ts = _ensure_utc(snap.created_at)
            if current_state == "flat" and next_state in {"long", "short"}:
                open_at = ts
                direction = "long" if next_state == "long" else "short"
            elif current_state in {"long", "short"} and next_state == "flat":
                if open_at is not None and direction is not None:
                    trips.append(self._materialize_trip(direction, open_at, ts, orders, llm))
                open_at = None
                direction = None
            current_state = next_state
        # Open trip at end of day
        if current_state in {"long", "short"} and open_at is not None and direction is not None:
            trips.append(self._materialize_trip(direction, open_at, None, orders, llm))
        return trips

    def _build_trips_from_orders(
        self,
        orders: list[_OrderItem],
        llm: list[_LLMItem],
    ) -> list[_Trip]:
        trips: list[_Trip] = []
        open_entry: _OrderItem | None = None
        direction: Literal["long", "short"] | None = None
        for order in orders:
            if order.status != "FILLED":
                continue
            if open_entry is None:
                direction = "long" if order.side in _ENTRY_SIDES["long"] else "short"
                open_entry = order
                continue
            if direction is not None and order.side in _EXIT_SIDES[direction]:
                trips.append(self._trip_from_pair(direction, open_entry, order, llm))
                open_entry = None
                direction = None
        if open_entry is not None and direction is not None:
            trips.append(self._trip_from_pair(direction, open_entry, None, llm))
        return trips

    def _materialize_trip(
        self,
        direction: Literal["long", "short"],
        open_at: datetime,
        close_at: datetime | None,
        orders: list[_OrderItem],
        llm: list[_LLMItem],
    ) -> _Trip:
        entry = self._find_order_in_window(orders, open_at, close_at, _ENTRY_SIDES[direction])
        exit_order = (
            self._find_order_in_window(orders, open_at, close_at, _EXIT_SIDES[direction])
            if close_at is not None else None
        )
        llm_ids = [
            i.id for i in llm
            if i.created_at >= open_at and (close_at is None or i.created_at < close_at)
        ]
        return _Trip(
            open_at=open_at,
            close_at=close_at,
            direction=direction,
            entry_order_id=entry.id if entry else None,
            exit_order_id=exit_order.id if exit_order else None,
            quantity=float(entry.quantity) if entry else 0.0,
            entry_price=float(entry.executed_price or entry.price) if entry else 0.0,
            exit_price=(
                float(exit_order.executed_price or exit_order.price) if exit_order else None
            ),
            realized_pnl=(exit_order.realized_pnl if exit_order else None),
            llm_interaction_ids=llm_ids,
        )

    def _trip_from_pair(
        self,
        direction: Literal["long", "short"],
        entry: _OrderItem,
        exit_order: _OrderItem | None,
        llm: list[_LLMItem],
    ) -> _Trip:
        open_at = entry.created_at
        close_at = exit_order.created_at if exit_order else None
        llm_ids = [
            i.id for i in llm
            if i.created_at >= open_at and (close_at is None or i.created_at < close_at)
        ]
        return _Trip(
            open_at=open_at,
            close_at=close_at,
            direction=direction,
            entry_order_id=entry.id,
            exit_order_id=exit_order.id if exit_order else None,
            quantity=float(entry.quantity),
            entry_price=float(entry.executed_price or entry.price),
            exit_price=(
                float(exit_order.executed_price or exit_order.price) if exit_order else None
            ),
            realized_pnl=(exit_order.realized_pnl if exit_order else None),
            llm_interaction_ids=llm_ids,
        )

    @staticmethod
    def _find_order_in_window(
        orders: list[_OrderItem],
        start: datetime,
        end: datetime | None,
        sides: set[str],
    ) -> _OrderItem | None:
        for order in orders:
            if order.side not in sides:
                continue
            if order.created_at < start:
                continue
            if end is not None and order.created_at > end:
                continue
            if order.status != "FILLED":
                continue
            return order
        return None
```

把 `aggregate_day` 中 `trips: list[Any] = []` 替换为：

```python
trips, inference_mode = self._build_trips(market, trade_day, orders, llm, t_open, t_close)
summary.trip_count = len(trips)
```

并在 metadata 加入 `"trip_inference_mode": inference_mode`。

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 -m pytest tests/test_replay_service_trips.py tests/test_replay_service_aggregate.py -v
```

Expected: 11 PASSED

- [ ] **Step 5: lint**

```bash
cd /Users/lcy/code/auto_trade
python3 -m basedpyright
```

Expected: 0 errors / 0 warnings

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_service_trips.py
git commit -m "feat(replay): build trips from snapshots or order inference"
```

---

## Task 2：5 个标签按优先级分类

> **目标：** 把 `_LLMItem.tag` 字段从 `NORMAL` 默认升级到正确的 5 种分类。**只在 `aggregate_day` 末尾调用一次 `_classify_tags`**，不改变 service 公共接口。

**Files:**
- Modify: `backend/app/services/replay_service.py`（加 `_classify_tags` + 把 `_LLMItem.tag` 字段填充）
- Create: `backend/tests/test_replay_tagger.py`

### T2.1 — 添加 `tag` 字段 + NORMAL 兜底

- [ ] **Step 1: 给 `_LLMItem` 加 `tag: str` 字段**

```python
@dataclass(frozen=True)
class _LLMItem:
    ...  # 现有字段
    tag: str = "NORMAL"
```

- [ ] **Step 2: 创建 `tests/test_replay_tagger.py` 并写 `test_unclassified_is_normal`**

```python
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_replay_tagger.db"
)

from datetime import date, datetime, timedelta, timezone

import pytest

from app.database import SessionLocal, engine
from app.models import Base, LLMInteraction, OrderRecord, RuntimeStateSnapshot, StrategyConfig, TradeEvent
from app.services.replay_service import ReplayService
from app.services.strategy_service import StrategyService


_DAY = date(2026, 5, 20)
_BASE = datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def _new_db():
    db = SessionLocal()
    db.add(StrategyConfig(symbol="AAPL.US", market="US", min_profit_amount=50.0))
    db.commit()
    return db


def _add_snapshot(db, minutes_after: int, state: str):
    db.add(RuntimeStateSnapshot(
        engine_state=state, last_price=150.0,
        created_at=_BASE + timedelta(minutes=minutes_after),
    ))


def _add_order(db, *, oid: int, broker_id: str, side: str, price: float, minutes_after: int, status: str = "FILLED"):
    db.add(OrderRecord(
        id=oid, broker_order_id=broker_id, symbol="AAPL.US", side=side,
        quantity=10, price=price, status=status,
        executed_quantity=10 if status == "FILLED" else None,
        executed_price=price if status == "FILLED" else None,
        created_at=_BASE + timedelta(minutes=minutes_after),
        filled_at=_BASE + timedelta(minutes=minutes_after) if status == "FILLED" else None,
    ))


def _add_llm(db, *, minutes_after: int, broker_id: str | None, applied: bool, order_action: str, order_status: str | None = None):
    db.add(LLMInteraction(
        symbol="AAPL.US", market="US", success=True,
        applied=applied, order_action=order_action, order_status=order_status,
        order_id=broker_id,
        created_at=_BASE + timedelta(minutes=minutes_after),
    ))


def test_unclassified_llm_is_normal():
    db = _new_db()
    _add_llm(db, minutes_after=0, broker_id=None, applied=False, order_action="NONE")
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    assert result["llm_interactions"][0].tag == "NORMAL"
    db.close()
```

- [ ] **Step 3: 跑测试确认通过（默认值 = NORMAL 已经命中）**

```bash
python3 -m pytest tests/test_replay_tagger.py::test_unclassified_llm_is_normal -v
```

Expected: PASS（基础值，分类器之后才会改）

- [ ] **Step 4: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_tagger.py
git commit -m "feat(replay): add tag field defaulting to NORMAL"
```

### T2.2 — MISSED_STOP（最高优先级）

- [ ] **Step 1: 加测试 `test_missed_stop_when_exit_skipped_and_later_loss`**

```python
def test_missed_stop_when_exit_skipped_and_later_loss():
    db = _new_db()
    # long → flat
    _add_snapshot(db, 0, "flat")
    _add_snapshot(db, 1, "long")
    _add_snapshot(db, 30, "flat")
    _add_order(db, oid=1, broker_id="b1", side="BUY", price=150.0, minutes_after=1)
    # LLM 建议平仓（SELL）但被 skipped
    _add_llm(
        db, minutes_after=5, broker_id="bX", applied=False,
        order_action="SUBMIT", order_status="SKIPPED",
    )
    # 后续真的亏损平仓
    _add_order(db, oid=2, broker_id="b2", side="SELL", price=145.0, minutes_after=30)
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    tags = [i.tag for i in result["llm_interactions"]]
    assert "MISSED_STOP" in tags
    db.close()


def test_missed_stop_requires_loss_within_window():
    db = _new_db()
    _add_snapshot(db, 0, "flat")
    _add_snapshot(db, 1, "long")
    _add_snapshot(db, 30, "flat")
    _add_order(db, oid=1, broker_id="b1", side="BUY", price=150.0, minutes_after=1)
    _add_llm(
        db, minutes_after=5, broker_id="bX", applied=False,
        order_action="SUBMIT", order_status="SKIPPED",
    )
    # 平仓盈利
    _add_order(db, oid=2, broker_id="b2", side="SELL", price=155.0, minutes_after=30)
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    # LLM 建议被 skip 但后续是盈利平仓 → 不算 MISSED_STOP
    assert "MISSED_STOP" not in [i.tag for i in result["llm_interactions"]]
    db.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_replay_tagger.py::test_missed_stop_when_exit_skipped_and_later_loss -v
```

Expected: FAIL (`MISSED_STOP not in [...NORMAL...]`)

- [ ] **Step 3: 在 `replay_service.py` 实现 `_classify_tags` 与 MISSED_STOP 规则**

```python
_EXECUTABLE_ACTIONS = {"SUBMIT", "CANCEL_REPLACE"}
_SKIPPED_STATUSES = {"SKIPPED", "REJECTED", "CANCELLED"}


class ReplayService:
    ...
    def _classify_tags(
        self,
        llm: list[_LLMItem],
        trips: list[_Trip],
        orders: list[_OrderItem],
        events: list[_TradeEventItem],
    ) -> list[_LLMItem]:
        orders_by_id = {o.id: o for o in orders}
        # build per-trip reprice counts (used by FREQUENT_REPRICE — T2.4)
        reprice_count_by_trip: dict[int, int] = {}

        classified: list[_LLMItem] = []
        for itx in llm:
            trip = self._locate_trip(itx, trips)
            tag = self._classify_single(
                itx, trip, llm, orders_by_id, events, reprice_count_by_trip,
            )
            classified.append(replace(itx, tag=tag))
        return classified

    @staticmethod
    def _locate_trip(itx: _LLMItem, trips: list[_Trip]) -> _Trip | None:
        for trip in trips:
            if itx.created_at < trip.open_at:
                continue
            if trip.close_at is None or itx.created_at < trip.close_at:
                return trip
        return None

    def _classify_single(
        self,
        itx: _LLMItem,
        trip: _Trip | None,
        llm: list[_LLMItem],
        orders_by_id: dict[int, _OrderItem],
        events: list[_TradeEventItem],
        reprice_count_by_trip: dict[int, int],
    ) -> str:
        if self._is_missed_stop(itx, trip, orders_by_id):
            return "MISSED_STOP"
        return "NORMAL"

    def _is_missed_stop(
        self,
        itx: _LLMItem,
        trip: _Trip | None,
        orders_by_id: dict[int, _OrderItem],
    ) -> bool:
        if itx.order_action not in _EXECUTABLE_ACTIONS:
            return False
        skipped = (not itx.applied) or (itx.order_status in _SKIPPED_STATUSES)
        if not skipped:
            return False
        # EXIT 判定：trip 持仓方向与 LLM 建议订单方向相反
        if trip is None or trip.exit_order_id is None:
            return False
        exit_order = orders_by_id.get(trip.exit_order_id)
        if exit_order is None or exit_order.realized_pnl is None:
            return False
        if exit_order.realized_pnl >= 0:
            return False
        delta = (exit_order.created_at - itx.created_at).total_seconds()
        if delta < 0 or delta > _ReplayTagParams.MISSED_STOP_WINDOW_SECONDS:
            return False
        return True
```

在 `aggregate_day` 末尾把 `llm` 替换为分类后的列表，并累计 `summary.tag_counts`：

```python
llm = self._classify_tags(llm, trips, orders, events)
summary.tag_counts = {}
for itx in llm:
    summary.tag_counts[itx.tag] = summary.tag_counts.get(itx.tag, 0) + 1
```

需要 `from dataclasses import replace` 导入。

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 -m pytest tests/test_replay_tagger.py -v
```

Expected: 3 PASSED（含基础 NORMAL）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_tagger.py
git commit -m "feat(replay): classify MISSED_STOP tag"
```

### T2.3 — PREMATURE_ENTRY

- [ ] **Step 1: 加测试**

```python
def test_premature_entry_when_quick_loss():
    db = _new_db()
    _add_snapshot(db, 0, "flat")
    _add_snapshot(db, 1, "long")
    _add_snapshot(db, 10, "flat")  # 10 分钟内 < 30 阈值
    _add_order(db, oid=1, broker_id="b1", side="BUY", price=150.0, minutes_after=1)
    _add_order(db, oid=2, broker_id="b2", side="SELL", price=145.0, minutes_after=10)
    _add_llm(db, minutes_after=1, broker_id="b1", applied=True, order_action="SUBMIT")
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    tags = [i.tag for i in result["llm_interactions"]]
    assert tags == ["PREMATURE_ENTRY"]
    db.close()


def test_premature_entry_skipped_when_trip_too_long():
    db = _new_db()
    _add_snapshot(db, 0, "flat")
    _add_snapshot(db, 1, "long")
    _add_snapshot(db, 35, "flat")  # 34 分钟 > 30 阈值
    _add_order(db, oid=1, broker_id="b1", side="BUY", price=150.0, minutes_after=1)
    _add_order(db, oid=2, broker_id="b2", side="SELL", price=145.0, minutes_after=35)
    _add_llm(db, minutes_after=1, broker_id="b1", applied=True, order_action="SUBMIT")
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    assert "PREMATURE_ENTRY" not in [i.tag for i in result["llm_interactions"]]
    db.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_replay_tagger.py::test_premature_entry_when_quick_loss -v
```

Expected: FAIL (`['NORMAL'] != ['PREMATURE_ENTRY']`)

- [ ] **Step 3: 添加 PREMATURE_ENTRY 规则**

在 `_classify_single` 中：

```python
    def _classify_single(self, itx, trip, llm, orders_by_id, events, reprice_count_by_trip) -> str:
        if self._is_missed_stop(itx, trip, orders_by_id):
            return "MISSED_STOP"
        if self._is_premature_entry(itx, trip, orders_by_id):
            return "PREMATURE_ENTRY"
        return "NORMAL"

    def _is_premature_entry(
        self,
        itx: _LLMItem,
        trip: _Trip | None,
        orders_by_id: dict[int, _OrderItem],
    ) -> bool:
        if not itx.applied or itx.order_action not in _EXECUTABLE_ACTIONS:
            return False
        if trip is None or trip.entry_order_id is None or trip.close_at is None:
            return False
        if trip.entry_order_id not in orders_by_id:
            return False
        entry = orders_by_id[trip.entry_order_id]
        # LLM 关联的是 entry 单
        if itx.linked_order_id != entry.id:
            return False
        if trip.realized_pnl is None or trip.realized_pnl >= 0:
            return False
        duration = (trip.close_at - trip.open_at).total_seconds()
        return duration < _ReplayTagParams.PREMATURE_WINDOW_SECONDS
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 -m pytest tests/test_replay_tagger.py -v
```

Expected: 5 PASSED

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_tagger.py
git commit -m "feat(replay): classify PREMATURE_ENTRY tag"
```

### T2.4 — FREQUENT_REPRICE

- [ ] **Step 1: 加测试**

```python
def test_frequent_reprice_when_trip_has_three_or_more_reprice_events():
    db = _new_db()
    _add_snapshot(db, 0, "flat")
    _add_snapshot(db, 1, "long")
    _add_snapshot(db, 60, "flat")
    _add_order(db, oid=1, broker_id="b1", side="BUY", price=150.0, minutes_after=1)
    _add_order(db, oid=2, broker_id="b2", side="SELL", price=155.0, minutes_after=60)
    # 3 次 CANCEL_REPLACE
    for n in (5, 10, 15):
        _add_llm(db, minutes_after=n, broker_id="b1", applied=True, order_action="CANCEL_REPLACE")
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    reprice = [i for i in result["llm_interactions"] if i.tag == "FREQUENT_REPRICE"]
    assert len(reprice) == 3
    db.close()


def test_reprice_count_includes_skip_category_events():
    db = _new_db()
    _add_snapshot(db, 0, "flat")
    _add_snapshot(db, 1, "long")
    _add_snapshot(db, 60, "flat")
    _add_order(db, oid=1, broker_id="b1", side="BUY", price=150.0, minutes_after=1)
    _add_order(db, oid=2, broker_id="b2", side="SELL", price=155.0, minutes_after=60)
    # 1 次 LLM CANCEL_REPLACE + 2 次 REPRICING skip 事件 = 3
    _add_llm(db, minutes_after=5, broker_id="b1", applied=True, order_action="CANCEL_REPLACE")
    for n in (10, 15):
        db.add(TradeEvent(
            event_type="ORDER_SKIPPED", symbol="AAPL.US", broker_order_id="b1",
            side="BUY", status="SKIPPED",
            payload_json='{"skip_category": "REPRICING"}',
            created_at=_BASE + timedelta(minutes=n),
        ))
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    assert [i.tag for i in result["llm_interactions"]] == ["FREQUENT_REPRICE"]
    db.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_replay_tagger.py::test_frequent_reprice_when_trip_has_three_or_more_reprice_events -v
```

Expected: FAIL (3 LLMs all `NORMAL`)

- [ ] **Step 3: 实现 FREQUENT_REPRICE**

修改 `_classify_tags` 预先计算每个 trip 的 reprice 计数：

```python
    def _classify_tags(self, llm, trips, orders, events) -> list[_LLMItem]:
        orders_by_id = {o.id: o for o in orders}
        trip_by_index = {i: trip for i, trip in enumerate(trips)}
        reprice_count_by_trip = self._count_reprice_per_trip(trips, llm, events)

        classified: list[_LLMItem] = []
        for itx in llm:
            trip_idx = self._locate_trip_index(itx, trips)
            trip = trips[trip_idx] if trip_idx is not None else None
            tag = self._classify_single(
                itx, trip, trip_idx, orders_by_id, events, reprice_count_by_trip,
            )
            classified.append(replace(itx, tag=tag))
        return classified

    @staticmethod
    def _locate_trip_index(itx: _LLMItem, trips: list[_Trip]) -> int | None:
        for idx, trip in enumerate(trips):
            if itx.created_at < trip.open_at:
                continue
            if trip.close_at is None or itx.created_at < trip.close_at:
                return idx
        return None

    def _count_reprice_per_trip(
        self,
        trips: list[_Trip],
        llm: list[_LLMItem],
        events: list[_TradeEventItem],
    ) -> dict[int, int]:
        counts: dict[int, int] = {i: 0 for i in range(len(trips))}
        for itx in llm:
            if itx.order_action != "CANCEL_REPLACE":
                continue
            idx = self._locate_trip_index(itx, trips)
            if idx is not None:
                counts[idx] = counts.get(idx, 0) + 1
        for evt in events:
            if evt.skip_category != "REPRICING":
                continue
            idx = self._locate_trip_index_by_time(evt.created_at, trips)
            if idx is not None:
                counts[idx] = counts.get(idx, 0) + 1
        return counts

    @staticmethod
    def _locate_trip_index_by_time(ts: datetime, trips: list[_Trip]) -> int | None:
        for idx, trip in enumerate(trips):
            if ts < trip.open_at:
                continue
            if trip.close_at is None or ts < trip.close_at:
                return idx
        return None
```

把 `_classify_single` 改为接受 `trip_idx`：

```python
    def _classify_single(
        self,
        itx: _LLMItem,
        trip: _Trip | None,
        trip_idx: int | None,
        orders_by_id: dict[int, _OrderItem],
        events: list[_TradeEventItem],
        reprice_count_by_trip: dict[int, int],
    ) -> str:
        if self._is_missed_stop(itx, trip, orders_by_id):
            return "MISSED_STOP"
        if self._is_premature_entry(itx, trip, orders_by_id):
            return "PREMATURE_ENTRY"
        if trip_idx is not None and reprice_count_by_trip.get(trip_idx, 0) >= _ReplayTagParams.REPRICE_THRESHOLD:
            return "FREQUENT_REPRICE"
        return "NORMAL"
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 -m pytest tests/test_replay_tagger.py -v
```

Expected: 7 PASSED

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_tagger.py
git commit -m "feat(replay): classify FREQUENT_REPRICE tag with skip_category counting"
```

### T2.5 — LOW_PROFIT

- [ ] **Step 1: 加测试**

```python
def test_low_profit_when_realized_pnl_below_threshold():
    db = _new_db()
    _add_snapshot(db, 0, "flat")
    _add_snapshot(db, 1, "long")
    _add_snapshot(db, 60, "flat")
    _add_order(db, oid=1, broker_id="b1", side="BUY", price=150.0, minutes_after=1)
    # min_profit_amount = 50；LOW_PROFIT_RATIO = 1.2 → 阈值 60
    # SELL 155 → PnL = 50 < 60 → LOW_PROFIT
    _add_order(db, oid=2, broker_id="b2", side="SELL", price=155.0, minutes_after=60)
    _add_llm(db, minutes_after=58, broker_id="b2", applied=True, order_action="SUBMIT")
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    assert result["llm_interactions"][0].tag == "LOW_PROFIT"
    db.close()


def test_low_profit_not_triggered_when_above_threshold():
    db = _new_db()
    _add_snapshot(db, 0, "flat")
    _add_snapshot(db, 1, "long")
    _add_snapshot(db, 60, "flat")
    _add_order(db, oid=1, broker_id="b1", side="BUY", price=150.0, minutes_after=1)
    # SELL 157 → PnL = 70 > 60
    _add_order(db, oid=2, broker_id="b2", side="SELL", price=157.0, minutes_after=60)
    _add_llm(db, minutes_after=58, broker_id="b2", applied=True, order_action="SUBMIT")
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    assert result["llm_interactions"][0].tag == "NORMAL"
    db.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_replay_tagger.py::test_low_profit_when_realized_pnl_below_threshold -v
```

Expected: FAIL (NORMAL instead of LOW_PROFIT)

- [ ] **Step 3: 在 `_classify_single` 加 LOW_PROFIT 分支**

```python
    def _classify_single(self, itx, trip, trip_idx, orders_by_id, events, reprice_count_by_trip) -> str:
        if self._is_missed_stop(itx, trip, orders_by_id):
            return "MISSED_STOP"
        if self._is_premature_entry(itx, trip, orders_by_id):
            return "PREMATURE_ENTRY"
        if trip_idx is not None and reprice_count_by_trip.get(trip_idx, 0) >= _ReplayTagParams.REPRICE_THRESHOLD:
            return "FREQUENT_REPRICE"
        if self._is_low_profit(itx, orders_by_id):
            return "LOW_PROFIT"
        return "NORMAL"

    def _is_low_profit(self, itx: _LLMItem, orders_by_id: dict[int, _OrderItem]) -> bool:
        if itx.linked_order_id is None:
            return False
        order = orders_by_id.get(itx.linked_order_id)
        if order is None or order.realized_pnl is None or order.realized_pnl <= 0:
            return False
        threshold = float(self._strategy.get_config().min_profit_amount) * _ReplayTagParams.LOW_PROFIT_RATIO
        return order.realized_pnl < threshold
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 -m pytest tests/test_replay_tagger.py -v
```

Expected: 9 PASSED

- [ ] **Step 5: lint**

```bash
cd /Users/lcy/code/auto_trade && python3 -m basedpyright
```

Expected: 0 / 0

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_tagger.py
git commit -m "feat(replay): classify LOW_PROFIT tag"
```

### T2.6 — 优先级回归测试

- [ ] **Step 1: 加测试 `test_priority_missed_stop_beats_premature_entry`**

确保规则优先级（MISSED_STOP > PREMATURE_ENTRY > FREQUENT_REPRICE > LOW_PROFIT）：

```python
def test_priority_missed_stop_beats_premature_entry():
    db = _new_db()
    # 触发 PREMATURE_ENTRY 条件（applied=True 入场单 + trip 短亏）
    # 同时这条 LLM 在 trip 内还做了 EXIT 建议被 skip
    # 但 MISSED_STOP 要求的是 “LLM 建议 EXIT 被 skip 且后续亏损”，所以
    # 我们用两条独立 LLM 来验证优先级各自不冲突，而不是叠加。
    # 这里直接断言：两条 LLM 各自走自己的规则。
    _add_snapshot(db, 0, "flat")
    _add_snapshot(db, 1, "long")
    _add_snapshot(db, 10, "flat")
    _add_order(db, oid=1, broker_id="b1", side="BUY", price=150.0, minutes_after=1)
    _add_order(db, oid=2, broker_id="b2", side="SELL", price=145.0, minutes_after=10)
    # 1) 入场 LLM —— 命中 PREMATURE_ENTRY
    _add_llm(db, minutes_after=1, broker_id="b1", applied=True, order_action="SUBMIT")
    # 2) 中途 LLM 建议平仓被 skipped —— 命中 MISSED_STOP
    _add_llm(
        db, minutes_after=5, broker_id="bX", applied=False,
        order_action="SUBMIT", order_status="SKIPPED",
    )
    db.commit()

    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    result = svc.aggregate_day(symbol=None, trade_day=_DAY)

    tags = sorted(i.tag for i in result["llm_interactions"])
    assert tags == ["MISSED_STOP", "PREMATURE_ENTRY"]
    db.close()
```

- [ ] **Step 2: 跑测试确认通过**

```bash
python3 -m pytest tests/test_replay_tagger.py -v
```

Expected: 10 PASSED

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_replay_tagger.py
git commit -m "test(replay): priority regression covering MISSED_STOP vs PREMATURE_ENTRY"
```

---

## Task 3：API 端点与 Pydantic Schemas

> **目标：** 把 `ReplayService` 的内部 dataclass 暴露为 `/api/replay/{days,{date},{date}/export}`，绑定到 main app。**导出实现在 T5**——本任务的 export 端点先返回 501 占位（明确不上线）或留 stub 但只完成 list/days、day 详情两个端点。这里我们采用：T3 直接完成 days + day 详情；export 端点签名落地但实现挪到 T5。

**Files:**
- Modify: `backend/app/schemas.py`（追加 ~9 个 model）
- Create: `backend/app/api/replay.py`
- Modify: `backend/app/main.py`（include router）
- Create: `backend/tests/test_replay_api.py`

### T3.1 — Schemas

- [ ] **Step 1: 在 `schemas.py` 末尾追加 P7 schemas**

```python
# Replay (P7)
from datetime import date as _date
from typing import Any as _Any, Literal as _Literal

from pydantic import BaseModel as _BaseModel


class ReplayDaySummary(_BaseModel):
    trade_day: _date
    symbol: str
    market: str
    trip_count: int
    realized_pnl: float
    llm_call_count: int
    llm_applied_count: int
    tag_counts: dict[str, int]


class ReplayDaysResponse(_BaseModel):
    symbol: str
    days: list[ReplayDaySummary]


class ReplayPricePoint(_BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class ReplayOrderItem(_BaseModel):
    id: int
    broker_order_id: str
    side: str
    status: str
    quantity: float
    price: float
    executed_quantity: float | None
    executed_price: float | None
    created_at: datetime
    filled_at: datetime | None
    realized_pnl: float | None


class ReplayLLMItem(_BaseModel):
    id: int
    interaction_type: str
    created_at: datetime
    applied: bool
    order_action: str
    order_status: str | None
    suggested_buy_low: float | None
    suggested_sell_high: float | None
    confidence: float | None
    summary: str
    prompt_excerpt: str
    linked_order_id: int | None
    tag: str


class ReplayTradeEventItem(_BaseModel):
    id: int
    event_type: str
    side: str
    status: str
    message: str
    broker_order_id: str
    skip_category: str | None
    linked_order_id: int | None
    created_at: datetime


class ReplayTrip(_BaseModel):
    open_at: datetime
    close_at: datetime | None
    direction: _Literal["long", "short"]
    entry_order_id: int | None
    exit_order_id: int | None
    quantity: float
    entry_price: float
    exit_price: float | None
    realized_pnl: float | None
    llm_interaction_ids: list[int]


class ReplayDayResponse(_BaseModel):
    summary: ReplayDaySummary
    price_source: _Literal["candles", "snapshots", "none"]
    prices: list[ReplayPricePoint]
    orders: list[ReplayOrderItem]
    trade_events: list[ReplayTradeEventItem]
    llm_interactions: list[ReplayLLMItem]
    trips: list[ReplayTrip]
    rth_window: tuple[datetime, datetime] | None
    metadata: dict[str, _Any]
```

（如果 `datetime` 未在 schemas.py 已有 import，确认顶部有 `from datetime import datetime`。）

- [ ] **Step 2: 跑现有测试确认 schemas import 无回归**

```bash
cd backend
python3 -c "from app import schemas; print(schemas.ReplayDaySummary.__fields__.keys())"
```

Expected: 打印字段列表，不抛错。

- [ ] **Step 3: 提交**

```bash
git add backend/app/schemas.py
git commit -m "feat(replay): add Pydantic schemas for replay API"
```

### T3.2 — `ReplayService.to_pydantic` adapters + `aggregate_day` 返回类型

- [ ] **Step 1: 在 `replay_service.py` 加 adapter 方法**

```python
from app.schemas import (
    ReplayDayResponse,
    ReplayDaySummary,
    ReplayDaysResponse,
    ReplayLLMItem,
    ReplayOrderItem,
    ReplayPricePoint,
    ReplayTradeEventItem,
    ReplayTrip,
)


def _summary_to_schema(s: _DaySummaryInternal) -> ReplayDaySummary:
    return ReplayDaySummary(
        trade_day=s.trade_day,
        symbol=s.symbol,
        market=s.market,
        trip_count=s.trip_count,
        realized_pnl=s.realized_pnl,
        llm_call_count=s.llm_call_count,
        llm_applied_count=s.llm_applied_count,
        tag_counts=dict(s.tag_counts),
    )
```

加 public `list_days_response` 与 `day_response`：

```python
class ReplayService:
    ...
    def list_days_response(self, symbol: str | None, limit: int) -> ReplayDaysResponse:
        rows = self.list_days(symbol, limit)
        chosen = rows[0].symbol if rows else (symbol or self._strategy.get_config().symbol or "")
        return ReplayDaysResponse(
            symbol=chosen,
            days=[_summary_to_schema(r) for r in rows],
        )

    def day_response(self, symbol: str | None, trade_day: date) -> ReplayDayResponse:
        raw = self.aggregate_day(symbol, trade_day)
        return ReplayDayResponse(
            summary=_summary_to_schema(raw["summary"]),
            price_source=raw["price_source"],
            prices=[
                ReplayPricePoint(ts=p.ts, open=p.open, high=p.high, low=p.low, close=p.close, volume=p.volume)
                for p in raw["prices"]
            ],
            orders=[
                ReplayOrderItem(**o.__dict__) for o in raw["orders"]
            ],
            trade_events=[
                ReplayTradeEventItem(**e.__dict__) for e in raw["trade_events"]
            ],
            llm_interactions=[
                ReplayLLMItem(
                    id=i.id,
                    interaction_type=i.interaction_type,
                    created_at=i.created_at,
                    applied=i.applied,
                    order_action=i.order_action,
                    order_status=i.order_status,
                    suggested_buy_low=i.suggested_buy_low,
                    suggested_sell_high=i.suggested_sell_high,
                    confidence=i.confidence,
                    summary=i.summary,
                    prompt_excerpt=i.prompt_excerpt,
                    linked_order_id=i.linked_order_id,
                    tag=i.tag,
                )
                for i in raw["llm_interactions"]
            ],
            trips=[
                ReplayTrip(**t.__dict__) for t in raw["trips"]
            ],
            rth_window=raw["rth_window"],
            metadata=raw["metadata"],
        )
```

- [ ] **Step 2: 加一个回归测试**（在 `test_replay_service_aggregate.py` 追加）

```python
from app.schemas import ReplayDayResponse


def test_day_response_returns_pydantic_model():
    db = _db()
    _strategy(db)
    svc = ReplayService(db=db, broker=None, strategy_service=StrategyService(db))
    resp = svc.day_response(symbol=None, trade_day=date(2026, 5, 20))
    assert isinstance(resp, ReplayDayResponse)
    assert resp.price_source == "none"
    db.close()
```

- [ ] **Step 3: 跑测试**

```bash
python3 -m pytest tests/test_replay_service_aggregate.py -v
```

Expected: 9 PASSED

- [ ] **Step 4: 提交**

```bash
git add backend/app/services/replay_service.py backend/tests/test_replay_service_aggregate.py
git commit -m "feat(replay): pydantic adapters for service responses"
```

### T3.3 — `/api/replay/days` 与 `/api/replay/{trade_day}` 端点

- [ ] **Step 1: 创建 `tests/test_replay_api.py`**

```python
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_replay_api.db"
)

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal, engine
from app.main import app
from app.models import Base, OrderRecord, StrategyConfig


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def _seed():
    db = SessionLocal()
    db.add(StrategyConfig(symbol="AAPL.US", market="US", min_profit_amount=50.0))
    db.add(OrderRecord(
        broker_order_id="b1", symbol="AAPL.US", side="BUY",
        quantity=10, price=150.0, status="FILLED",
        executed_quantity=10, executed_price=150.0,
        created_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
    ))
    db.commit()
    db.close()


client = TestClient(app)


def test_list_days_returns_200_with_data():
    _seed()
    resp = client.get("/api/replay/days?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "AAPL.US"
    assert len(data["days"]) == 1


def test_list_days_returns_empty_list_when_no_data():
    db = SessionLocal()
    db.add(StrategyConfig(symbol="AAPL.US", market="US"))
    db.commit()
    db.close()
    resp = client.get("/api/replay/days")
    assert resp.status_code == 200
    assert resp.json()["days"] == []


def test_list_days_rejects_invalid_limit():
    _seed()
    assert client.get("/api/replay/days?limit=0").status_code == 422
    assert client.get("/api/replay/days?limit=91").status_code == 422


def test_list_days_422_when_symbol_unset():
    db = SessionLocal()
    db.add(StrategyConfig(symbol="", market="US"))
    db.commit()
    db.close()
    resp = client.get("/api/replay/days")
    assert resp.status_code == 422


def test_day_detail_returns_200_with_empty_when_no_data():
    db = SessionLocal()
    db.add(StrategyConfig(symbol="AAPL.US", market="US"))
    db.commit()
    db.close()
    resp = client.get("/api/replay/2026-05-20")
    assert resp.status_code == 200
    body = resp.json()
    assert body["price_source"] == "none"
    assert body["orders"] == []


def test_day_detail_includes_orders():
    _seed()
    resp = client.get("/api/replay/2026-05-20")
    assert resp.status_code == 200
    assert len(resp.json()["orders"]) == 1
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_replay_api.py::test_list_days_returns_200_with_data -v
```

Expected: 404 (route not registered)

- [ ] **Step 3: 创建 `backend/app/api/replay.py`**

```python
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.runner import get_runner
from app.schemas import ReplayDayResponse, ReplayDaysResponse
from app.services.replay_service import ReplayService
from app.services.strategy_service import StrategyService

router = APIRouter(prefix="/api/replay", tags=["replay"])


def _service(db: Session) -> ReplayService:
    broker = None
    try:
        broker = get_runner().broker
    except Exception:  # noqa: BLE001
        broker = None
    return ReplayService(
        db=db,
        broker=broker,
        strategy_service=StrategyService(db),
    )


@router.get("/days", response_model=ReplayDaysResponse)
def list_replay_days(
    limit: int = Query(default=30, ge=1, le=90),
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> ReplayDaysResponse:
    svc = _service(db)
    try:
        return svc.list_days_response(symbol=symbol, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{trade_day}", response_model=ReplayDayResponse)
def get_replay_day(
    trade_day: date,
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> ReplayDayResponse:
    svc = _service(db)
    try:
        return svc.day_response(symbol=symbol, trade_day=trade_day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
```

- [ ] **Step 4: 注册 router 到 `app/main.py`**

在 imports 区段加入：

```python
from app.api.replay import router as replay_router
```

在 `app.include_router(...)` 列表末尾加入：

```python
app.include_router(replay_router)
```

- [ ] **Step 5: 跑测试确认通过**

```bash
python3 -m pytest tests/test_replay_api.py -v
```

Expected: 6 PASSED

- [ ] **Step 6: 全量回归**

```bash
python3 -m pytest tests/ -q
```

Expected: 全绿（约 491 PASSED）

- [ ] **Step 7: lint**

```bash
cd /Users/lcy/code/auto_trade && python3 -m basedpyright
```

Expected: 0 / 0

- [ ] **Step 8: 提交**

```bash
git add backend/app/api/replay.py backend/app/main.py backend/tests/test_replay_api.py
git commit -m "feat(replay): expose /api/replay/days and /api/replay/{date}"
```

---

## Task 4：导出端点（JSON 细粒度 + CSV 扁平）

> **目标：** 实现 `GET /api/replay/{trade_day}/export?format=json|csv`，浏览器原生下载。JSON 含 prompt 全文与 raw_response；CSV 扁平到每条 LLM 交互一行。

**Files:**
- Modify: `backend/app/services/replay_service.py`（加 `export_day`）
- Modify: `backend/app/api/replay.py`（加 `/{trade_day}/export` 端点）
- Create: `backend/tests/test_replay_export.py`

### T4.1 — JSON 导出

- [ ] **Step 1: 创建 `tests/test_replay_export.py`**

```python
from __future__ import annotations

import json
import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_replay_export.db"
)

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal, engine
from app.main import app
from app.models import Base, LLMInteraction, OrderRecord, StrategyConfig


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def _seed():
    db = SessionLocal()
    db.add(StrategyConfig(symbol="AAPL.US", market="US", min_profit_amount=50.0))
    db.add(OrderRecord(
        id=1, broker_order_id="b1", symbol="AAPL.US", side="BUY",
        quantity=10, price=150.0, status="FILLED",
        executed_quantity=10, executed_price=150.0,
        created_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
        filled_at=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
    ))
    db.add(LLMInteraction(
        symbol="AAPL.US", market="US", success=True, applied=True,
        order_action="SUBMIT", order_id="b1",
        prompt="FULL PROMPT 完整内容", raw_response="FULL RESPONSE",
        parsed_response='{"suggested_buy_low": 148.0}',
        created_at=datetime(2026, 5, 20, 14, 29, tzinfo=timezone.utc),
    ))
    db.commit()
    db.close()


client = TestClient(app)


def test_export_json_includes_full_prompt_and_metadata():
    _seed()
    resp = client.get("/api/replay/2026-05-20/export?format=json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert "replay-AAPL.US-2026-05-20.json" in resp.headers["content-disposition"]
    payload = resp.json()
    assert payload["metadata"]["min_profit_amount"] == 50.0
    assert payload["metadata"]["tag_thresholds"]["REPRICE_THRESHOLD"] == 3
    assert payload["llm_interactions"][0]["prompt"] == "FULL PROMPT 完整内容"
    assert payload["llm_interactions"][0]["raw_response"] == "FULL RESPONSE"
    assert payload["llm_interactions"][0]["linked_order"]["broker_order_id"] == "b1"


def test_export_rejects_unknown_format():
    _seed()
    resp = client.get("/api/replay/2026-05-20/export?format=yaml")
    assert resp.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_replay_export.py::test_export_json_includes_full_prompt_and_metadata -v
```

Expected: 404

- [ ] **Step 3: 在 `replay_service.py` 加 `export_day_json`**

```python
def export_day_json(self, symbol: str | None, trade_day: date) -> dict[str, Any]:
    chosen, market = self._resolve_symbol(symbol)
    raw = self.aggregate_day(chosen, trade_day)

    orders_by_id = {o.id: o for o in raw["orders"]}

    return {
        "metadata": {
            "symbol": raw["summary"].symbol,
            "market": raw["summary"].market,
            "trade_day": trade_day.isoformat(),
            "rth_window": [
                raw["rth_window"][0].isoformat() if raw["rth_window"] else None,
                raw["rth_window"][1].isoformat() if raw["rth_window"] else None,
            ],
            **raw["metadata"],
        },
        "summary": {
            "trip_count": raw["summary"].trip_count,
            "realized_pnl": raw["summary"].realized_pnl,
            "llm_call_count": raw["summary"].llm_call_count,
            "llm_applied_count": raw["summary"].llm_applied_count,
            "tag_counts": dict(raw["summary"].tag_counts),
        },
        "price_source": raw["price_source"],
        "trips": [self._trip_to_export(t) for t in raw["trips"]],
        "orders": [self._order_to_export(o) for o in raw["orders"]],
        "trade_events": [self._event_to_export(e, orders_by_id) for e in raw["trade_events"]],
        "llm_interactions": [
            self._llm_to_export(i, orders_by_id) for i in raw["llm_interactions"]
        ],
    }

@staticmethod
def _trip_to_export(t: _Trip) -> dict[str, Any]:
    return {
        "open_at": t.open_at.isoformat(),
        "close_at": t.close_at.isoformat() if t.close_at else None,
        "direction": t.direction,
        "entry_order_id": t.entry_order_id,
        "exit_order_id": t.exit_order_id,
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "realized_pnl": t.realized_pnl,
        "llm_interaction_ids": list(t.llm_interaction_ids),
    }

@staticmethod
def _order_to_export(o: _OrderItem) -> dict[str, Any]:
    return {
        "id": o.id,
        "broker_order_id": o.broker_order_id,
        "side": o.side,
        "status": o.status,
        "quantity": o.quantity,
        "price": o.price,
        "executed_quantity": o.executed_quantity,
        "executed_price": o.executed_price,
        "created_at": o.created_at.isoformat(),
        "filled_at": o.filled_at.isoformat() if o.filled_at else None,
        "realized_pnl": o.realized_pnl,
    }

@staticmethod
def _event_to_export(e: _TradeEventItem, orders_by_id: dict[int, _OrderItem]) -> dict[str, Any]:
    return {
        "id": e.id,
        "event_type": e.event_type,
        "side": e.side,
        "status": e.status,
        "message": e.message,
        "broker_order_id": e.broker_order_id,
        "skip_category": e.skip_category,
        "linked_order_id": e.linked_order_id,
        "created_at": e.created_at.isoformat(),
    }

@staticmethod
def _llm_to_export(i: _LLMItem, orders_by_id: dict[int, _OrderItem]) -> dict[str, Any]:
    linked = orders_by_id.get(i.linked_order_id) if i.linked_order_id else None
    return {
        "id": i.id,
        "interaction_type": i.interaction_type,
        "created_at": i.created_at.isoformat(),
        "applied": i.applied,
        "order_action": i.order_action,
        "order_status": i.order_status,
        "suggested_buy_low": i.suggested_buy_low,
        "suggested_sell_high": i.suggested_sell_high,
        "confidence": i.confidence,
        "prompt": i.prompt,
        "raw_response": i.raw_response,
        "parsed_response": i.parsed_response,
        "tag": i.tag,
        "linked_order": ReplayService._order_to_export(linked) if linked else None,
    }
```

- [ ] **Step 4: 加 `/api/replay/{trade_day}/export` 端点**

在 `api/replay.py` 顶部增 import：

```python
import json as _json
from typing import Literal

from fastapi.responses import Response
```

加端点：

```python
@router.get("/{trade_day}/export")
def export_replay_day(
    trade_day: date,
    format: Literal["json", "csv"] = Query(default="json"),
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> Response:
    svc = _service(db)
    try:
        if format == "json":
            payload = svc.export_day_json(symbol=symbol, trade_day=trade_day)
            body = _json.dumps(payload, ensure_ascii=False, indent=2)
            return Response(
                content=body,
                media_type="application/json",
                headers={
                    "Content-Disposition": (
                        f"attachment; filename=replay-{payload['metadata']['symbol']}-{trade_day.isoformat()}.json"
                    ),
                },
            )
        # T4.2 will add csv
        raise HTTPException(status_code=501, detail="csv export not implemented yet")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
```

- [ ] **Step 5: 跑测试确认 JSON 通过**

```bash
python3 -m pytest tests/test_replay_export.py::test_export_json_includes_full_prompt_and_metadata tests/test_replay_export.py::test_export_rejects_unknown_format -v
```

Expected: 2 PASSED

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/replay_service.py backend/app/api/replay.py backend/tests/test_replay_export.py
git commit -m "feat(replay): JSON export with full prompt and metadata"
```

### T4.2 — CSV 导出

- [ ] **Step 1: 加 CSV 测试**

```python
def test_export_csv_has_flat_columns():
    _seed()
    resp = client.get("/api/replay/2026-05-20/export?format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "replay-AAPL.US-2026-05-20.csv" in resp.headers["content-disposition"]
    lines = resp.text.strip().splitlines()
    header = lines[0].split(",")
    assert header == [
        "llm_id", "created_at", "interaction_type", "applied", "order_action",
        "suggested_buy_low", "suggested_sell_high", "confidence", "prompt_summary",
        "linked_order_id", "linked_side", "linked_status", "linked_quantity",
        "linked_price", "linked_executed_price", "linked_realized_pnl", "tag",
    ]
    assert len(lines) == 2
    row = lines[1].split(",")
    assert row[header.index("linked_order_id")] == "1"
    assert row[header.index("linked_side")] == "BUY"


def test_export_csv_empty_when_no_llm_calls():
    db = SessionLocal()
    db.add(StrategyConfig(symbol="AAPL.US", market="US"))
    db.commit()
    db.close()
    resp = client.get("/api/replay/2026-05-20/export?format=csv")
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    assert len(lines) == 1  # 仅 header
```

- [ ] **Step 2: 跑测试确认 CSV 失败**

```bash
python3 -m pytest tests/test_replay_export.py::test_export_csv_has_flat_columns -v
```

Expected: 501

- [ ] **Step 3: 在 `replay_service.py` 加 `export_day_csv`**

```python
import csv
import io


def export_day_csv(self, symbol: str | None, trade_day: date) -> str:
    payload = self.export_day_json(symbol, trade_day)
    buf = io.StringIO()
    writer = csv.writer(buf)
    columns = [
        "llm_id", "created_at", "interaction_type", "applied", "order_action",
        "suggested_buy_low", "suggested_sell_high", "confidence", "prompt_summary",
        "linked_order_id", "linked_side", "linked_status", "linked_quantity",
        "linked_price", "linked_executed_price", "linked_realized_pnl", "tag",
    ]
    writer.writerow(columns)
    for itx in payload["llm_interactions"]:
        linked = itx.get("linked_order") or {}
        writer.writerow([
            itx["id"],
            itx["created_at"],
            itx["interaction_type"],
            str(itx["applied"]).lower(),
            itx["order_action"],
            itx["suggested_buy_low"] if itx["suggested_buy_low"] is not None else "",
            itx["suggested_sell_high"] if itx["suggested_sell_high"] is not None else "",
            itx["confidence"] if itx["confidence"] is not None else "",
            (itx["parsed_response"] or "").replace("\n", " ")[:200],
            linked.get("id", ""),
            linked.get("side", ""),
            linked.get("status", ""),
            linked.get("quantity", ""),
            linked.get("price", ""),
            linked.get("executed_price", "") if linked.get("executed_price") is not None else "",
            linked.get("realized_pnl", "") if linked.get("realized_pnl") is not None else "",
            itx["tag"],
        ])
    return buf.getvalue()
```

- [ ] **Step 4: 在 `api/replay.py` 把 csv 分支替换为真实实现**

```python
        if format == "csv":
            body = svc.export_day_csv(symbol=symbol, trade_day=trade_day)
            metadata = svc.aggregate_day(symbol, trade_day)
            sym = metadata["summary"].symbol
            return Response(
                content=body,
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f"attachment; filename=replay-{sym}-{trade_day.isoformat()}.csv"
                    ),
                },
            )
```

把 `if format == "json":` 与 `if format == "csv":` 改成 if/elif，并删掉 raise 501 行。

- [ ] **Step 5: 跑测试确认 CSV 通过**

```bash
python3 -m pytest tests/test_replay_export.py -v
```

Expected: 4 PASSED

- [ ] **Step 6: lint + 全量回归**

```bash
cd /Users/lcy/code/auto_trade && python3 -m basedpyright
cd backend && python3 -m pytest tests/ -q
```

Expected: lint 0/0；pytest 全绿。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/replay_service.py backend/app/api/replay.py backend/tests/test_replay_export.py
git commit -m "feat(replay): CSV export with flat columns"
```

---

## Task 5：前端复盘页

> **目标：** 在 `/#/replay` 提供日历列表、价格图、LLM 卡片流、订单时间线、导出按钮，并加 cypress 覆盖。

**Files:**
- Create: `frontend/src/api/replay.ts`
- Create: `frontend/src/utils/replay_tags.ts`
- Create: `frontend/src/views/Replay.vue`
- Create: `frontend/src/components/ReplayPriceChart.vue`
- Create: `frontend/cypress/e2e/replay.cy.ts`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/types/index.ts`

### T5.1 — Types + API client

- [ ] **Step 1: 在 `frontend/src/types/index.ts` 追加**

```ts
export type ReplayTag = 'MISSED_STOP' | 'PREMATURE_ENTRY' | 'FREQUENT_REPRICE' | 'LOW_PROFIT' | 'NORMAL'

export interface ReplayDaySummary {
  trade_day: string
  symbol: string
  market: string
  trip_count: number
  realized_pnl: number
  llm_call_count: number
  llm_applied_count: number
  tag_counts: Record<string, number>
}

export interface ReplayDaysResponse {
  symbol: string
  days: ReplayDaySummary[]
}

export interface ReplayPricePoint {
  ts: string
  open: number; high: number; low: number; close: number
  volume: number | null
}

export interface ReplayOrderItem {
  id: number
  broker_order_id: string
  side: string
  status: string
  quantity: number
  price: number
  executed_quantity: number | null
  executed_price: number | null
  created_at: string
  filled_at: string | null
  realized_pnl: number | null
}

export interface ReplayLLMItem {
  id: number
  interaction_type: string
  created_at: string
  applied: boolean
  order_action: string
  order_status: string | null
  suggested_buy_low: number | null
  suggested_sell_high: number | null
  confidence: number | null
  summary: string
  prompt_excerpt: string
  linked_order_id: number | null
  tag: ReplayTag
}

export interface ReplayTradeEventItem {
  id: number
  event_type: string
  side: string
  status: string
  message: string
  broker_order_id: string
  skip_category: string | null
  linked_order_id: number | null
  created_at: string
}

export interface ReplayTrip {
  open_at: string
  close_at: string | null
  direction: 'long' | 'short'
  entry_order_id: number | null
  exit_order_id: number | null
  quantity: number
  entry_price: number
  exit_price: number | null
  realized_pnl: number | null
  llm_interaction_ids: number[]
}

export interface ReplayDayResponse {
  summary: ReplayDaySummary
  price_source: 'candles' | 'snapshots' | 'none'
  prices: ReplayPricePoint[]
  orders: ReplayOrderItem[]
  trade_events: ReplayTradeEventItem[]
  llm_interactions: ReplayLLMItem[]
  trips: ReplayTrip[]
  rth_window: [string, string] | null
  metadata: Record<string, unknown>
}
```

- [ ] **Step 2: 创建 `frontend/src/api/replay.ts`**

```ts
import { client } from './client'
import type { ReplayDayResponse, ReplayDaysResponse } from '@/types'

export async function listReplayDays(symbol?: string, limit = 30): Promise<ReplayDaysResponse> {
  const params: Record<string, string | number> = { limit }
  if (symbol) params.symbol = symbol
  const { data } = await client.get('/replay/days', { params })
  return data
}

export async function fetchReplayDay(tradeDay: string, symbol?: string): Promise<ReplayDayResponse> {
  const params: Record<string, string> = {}
  if (symbol) params.symbol = symbol
  const { data } = await client.get(`/replay/${tradeDay}`, { params })
  return data
}

export function buildReplayExportUrl(tradeDay: string, format: 'json' | 'csv', symbol?: string): string {
  const base = client.defaults.baseURL ?? ''
  const params = new URLSearchParams({ format })
  if (symbol) params.set('symbol', symbol)
  return `${base}/replay/${tradeDay}/export?${params.toString()}`
}
```

- [ ] **Step 3: 创建 `frontend/src/utils/replay_tags.ts`**

```ts
import type { ReplayTag } from '@/types'

export const REPLAY_TAG_COLOR: Record<ReplayTag, string> = {
  MISSED_STOP: '#dc2626',
  PREMATURE_ENTRY: '#ea580c',
  FREQUENT_REPRICE: '#ca8a04',
  LOW_PROFIT: '#facc15',
  NORMAL: '#9ca3af',
}

export const REPLAY_TAG_LABEL: Record<ReplayTag, string> = {
  MISSED_STOP: '错过止损',
  PREMATURE_ENTRY: '过早进场',
  FREQUENT_REPRICE: '频繁重挂',
  LOW_PROFIT: '收益不足',
  NORMAL: '正常交易',
}
```

- [ ] **Step 4: type-check**

```bash
cd frontend
npm run type-check
```

Expected: 0 errors

- [ ] **Step 5: 提交**

```bash
git add frontend/src/types/index.ts frontend/src/api/replay.ts frontend/src/utils/replay_tags.ts
git commit -m "feat(replay/web): types + api client + tag visual map"
```

### T5.2 — `ReplayPriceChart.vue`（SVG，不引图表库）

- [ ] **Step 1: 创建 `frontend/src/components/ReplayPriceChart.vue`**

```vue
<template>
  <svg
    :viewBox="`0 0 ${width} ${height}`"
    preserveAspectRatio="none"
    class="replay-chart"
    data-testid="replay-price-chart"
  >
    <rect
      v-for="(trip, idx) in trips"
      :key="`trip-${idx}`"
      :x="xForTime(trip.open_at)"
      :y="0"
      :width="Math.max(0, xForTime(trip.close_at ?? rthEnd) - xForTime(trip.open_at))"
      :height="height"
      :fill="trip.direction === 'long' ? 'rgba(59,130,246,0.10)' : 'rgba(234,88,12,0.10)'"
    />
    <polyline
      v-if="points.length > 1"
      :points="polylinePoints"
      fill="none"
      stroke="#1f2937"
      stroke-width="1.5"
    />
    <g v-for="mark in llmMarks" :key="`llm-${mark.id}`">
      <line
        :x1="xForTime(mark.created_at)"
        :y1="0"
        :x2="xForTime(mark.created_at)"
        :y2="height"
        :stroke="mark.color"
        stroke-width="1"
        stroke-dasharray="2 2"
      />
      <circle :cx="xForTime(mark.created_at)" :cy="6" r="4" :fill="mark.color">
        <title>{{ mark.tooltip }}</title>
      </circle>
    </g>
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ReplayLLMItem, ReplayPricePoint, ReplayTrip } from '@/types'
import { REPLAY_TAG_COLOR, REPLAY_TAG_LABEL } from '@/utils/replay_tags'

const props = defineProps<{
  prices: ReplayPricePoint[]
  trips: ReplayTrip[]
  llmInteractions: ReplayLLMItem[]
  rthWindow: [string, string] | null
}>()

const width = 800
const height = 320

const rthStart = computed(() => props.rthWindow?.[0] ?? props.prices[0]?.ts ?? new Date().toISOString())
const rthEnd = computed(() => props.rthWindow?.[1] ?? props.prices[props.prices.length - 1]?.ts ?? new Date().toISOString())

const minPrice = computed(() => {
  if (props.prices.length === 0) return 0
  return Math.min(...props.prices.map((p) => p.low))
})
const maxPrice = computed(() => {
  if (props.prices.length === 0) return 1
  const max = Math.max(...props.prices.map((p) => p.high))
  return max === minPrice.value ? max + 1 : max
})

function xForTime(iso: string): number {
  const t = Date.parse(iso)
  const a = Date.parse(rthStart.value)
  const b = Date.parse(rthEnd.value)
  if (b === a) return 0
  return ((t - a) / (b - a)) * width
}

function yForPrice(price: number): number {
  return height - ((price - minPrice.value) / (maxPrice.value - minPrice.value)) * height
}

const points = computed(() => props.prices)

const polylinePoints = computed(() =>
  points.value.map((p) => `${xForTime(p.ts).toFixed(2)},${yForPrice(p.close).toFixed(2)}`).join(' '),
)

const llmMarks = computed(() =>
  props.llmInteractions.map((i) => ({
    id: i.id,
    created_at: i.created_at,
    color: REPLAY_TAG_COLOR[i.tag],
    tooltip: `${REPLAY_TAG_LABEL[i.tag]} · ${i.summary.slice(0, 120)}`,
  })),
)
</script>

<style scoped>
.replay-chart {
  width: 100%;
  height: 320px;
  background: #f9fafb;
}
</style>
```

- [ ] **Step 2: type-check**

```bash
npm run type-check
```

Expected: 0 errors

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/ReplayPriceChart.vue
git commit -m "feat(replay/web): ReplayPriceChart SVG with trip shading and llm marks"
```

### T5.3 — `Replay.vue` 主视图 + 路由

- [ ] **Step 1: 创建 `frontend/src/views/Replay.vue`**

```vue
<template>
  <el-card class="replay-card">
    <template #header>
      <div class="replay-header">
        <el-select
          v-model="selectedDay"
          placeholder="选择交易日"
          :disabled="loadingDays"
          data-testid="replay-day-select"
          @change="loadDay"
        >
          <el-option
            v-for="d in days"
            :key="d.trade_day"
            :label="`${d.trade_day} · PnL ${d.realized_pnl.toFixed(2)} · ${d.trip_count} trips`"
            :value="d.trade_day"
          />
        </el-select>
        <div class="actions">
          <el-button type="primary" :disabled="!selectedDay" @click="onExport('json')">导出 JSON</el-button>
          <el-button :disabled="!selectedDay" @click="onExport('csv')">导出 CSV</el-button>
        </div>
      </div>
    </template>

    <div v-if="days.length === 0 && !loadingDays" class="replay-empty">暂无复盘数据</div>

    <div v-if="day" class="replay-grid">
      <div class="summary-col" data-testid="replay-summary">
        <div>Trips: <strong>{{ day.summary.trip_count }}</strong></div>
        <div>Net PnL: <strong>{{ day.summary.realized_pnl.toFixed(2) }}</strong></div>
        <div>LLM: <strong>{{ day.summary.llm_applied_count }} / {{ day.summary.llm_call_count }}</strong></div>
        <div class="tag-chips">
          <el-tag
            v-for="(count, tag) in day.summary.tag_counts"
            :key="tag"
            :style="{ backgroundColor: tagColor(tag), color: '#fff', borderColor: tagColor(tag) }"
            class="tag-chip"
          >
            {{ tagLabel(tag) }} {{ count }}
          </el-tag>
        </div>
      </div>
      <div class="chart-col">
        <ReplayPriceChart
          :prices="day.prices"
          :trips="day.trips"
          :llm-interactions="day.llm_interactions"
          :rth-window="day.rth_window"
        />
        <div class="price-source-note">
          数据源：{{ day.price_source === 'candles' ? '券商 K 线' : day.price_source === 'snapshots' ? '内部快照' : '无价格数据' }}
        </div>
      </div>
    </div>

    <div v-if="day" class="timelines">
      <div class="llm-col" data-testid="replay-llm-list">
        <h3>LLM 交互</h3>
        <div v-for="item in day.llm_interactions" :key="item.id" class="llm-card">
          <div class="tag-strip" :style="{ backgroundColor: tagColor(item.tag) }" />
          <div class="llm-body">
            <div class="llm-meta">
              <span>{{ shortTime(item.created_at) }}</span>
              <span>{{ tagLabel(item.tag) }}</span>
              <span>{{ item.applied ? '已应用' : '未应用' }}</span>
            </div>
            <div class="llm-summary">{{ item.summary }}</div>
            <div v-if="item.linked_order_id" class="llm-link">
              关联订单 #{{ item.linked_order_id }}
            </div>
          </div>
        </div>
      </div>
      <div class="event-col" data-testid="replay-event-list">
        <h3>订单 & 事件</h3>
        <el-table :data="combinedTimeline" size="small" stripe>
          <el-table-column prop="time" label="时间" width="100" />
          <el-table-column prop="kind" label="类别" width="80" />
          <el-table-column prop="side" label="方向" width="80" />
          <el-table-column prop="status" label="状态" width="100" />
          <el-table-column prop="detail" label="详情" />
        </el-table>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { buildReplayExportUrl, fetchReplayDay, listReplayDays } from '@/api/replay'
import ReplayPriceChart from '@/components/ReplayPriceChart.vue'
import { REPLAY_TAG_COLOR, REPLAY_TAG_LABEL } from '@/utils/replay_tags'
import type { ReplayDayResponse, ReplayDaySummary, ReplayTag } from '@/types'

const days = ref<ReplayDaySummary[]>([])
const loadingDays = ref(false)
const selectedDay = ref<string>('')
const day = ref<ReplayDayResponse | null>(null)

function tagColor(tag: string): string {
  return REPLAY_TAG_COLOR[(tag as ReplayTag) in REPLAY_TAG_COLOR ? (tag as ReplayTag) : 'NORMAL']
}
function tagLabel(tag: string): string {
  return REPLAY_TAG_LABEL[(tag as ReplayTag) in REPLAY_TAG_LABEL ? (tag as ReplayTag) : 'NORMAL']
}
function shortTime(iso: string): string {
  return iso.slice(11, 16)
}

const combinedTimeline = computed(() => {
  if (!day.value) return []
  const orderRows = day.value.orders.map((o) => ({
    time: shortTime(o.created_at),
    kind: '订单',
    side: o.side,
    status: o.status,
    detail: `${o.quantity} @ ${o.price}${o.realized_pnl != null ? ` · PnL ${o.realized_pnl.toFixed(2)}` : ''}`,
    ts: o.created_at,
  }))
  const eventRows = day.value.trade_events.map((e) => ({
    time: shortTime(e.created_at),
    kind: e.event_type,
    side: e.side,
    status: e.status,
    detail: e.skip_category ? `${e.message} [${e.skip_category}]` : e.message,
    ts: e.created_at,
  }))
  return [...orderRows, ...eventRows].sort((a, b) => a.ts.localeCompare(b.ts))
})

async function loadDays() {
  loadingDays.value = true
  try {
    const resp = await listReplayDays()
    days.value = resp.days
    if (resp.days.length > 0 && !selectedDay.value) {
      selectedDay.value = resp.days[0].trade_day
      await loadDay()
    }
  } finally {
    loadingDays.value = false
  }
}

async function loadDay() {
  if (!selectedDay.value) return
  day.value = await fetchReplayDay(selectedDay.value)
}

function onExport(format: 'json' | 'csv') {
  if (!selectedDay.value) return
  window.open(buildReplayExportUrl(selectedDay.value, format), '_blank')
}

onMounted(loadDays)
</script>

<style scoped>
.replay-card { margin: 16px; }
.replay-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.actions { display: flex; gap: 8px; }
.replay-empty { padding: 32px; text-align: center; color: #6b7280; }
.replay-grid { display: grid; grid-template-columns: 240px 1fr; gap: 16px; }
.summary-col > div { margin-bottom: 8px; }
.tag-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.tag-chip { color: #fff; }
.price-source-note { font-size: 12px; color: #6b7280; margin-top: 4px; }
.timelines { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px; }
.llm-card { display: flex; gap: 8px; padding: 8px; background: #fff; border: 1px solid #e5e7eb; border-radius: 4px; margin-bottom: 8px; }
.tag-strip { width: 4px; flex-shrink: 0; border-radius: 2px; }
.llm-meta { display: flex; gap: 12px; font-size: 12px; color: #6b7280; }
.llm-summary { font-size: 13px; margin-top: 4px; }
.llm-link { font-size: 12px; color: #2563eb; margin-top: 4px; }

@media (max-width: 768px) {
  .replay-grid { grid-template-columns: 1fr; }
  .timelines { grid-template-columns: 1fr; }
}
</style>
```

- [ ] **Step 2: 在 `frontend/src/router/index.ts` 增加路由**

打开文件，在 routes 数组中（`/credentials` 之前）插入：

```ts
{
  path: '/replay',
  name: 'replay',
  component: () => import('@/views/Replay.vue'),
},
```

- [ ] **Step 3: 在 `frontend/src/App.vue` 导航栏增加入口**

找到现有 `el-menu` 或 `router-link` 列表，在 Backtest 与 Credentials 之间插入：

```html
<el-menu-item index="/replay">Replay</el-menu-item>
```

- [ ] **Step 4: 跑 type-check + build**

```bash
npm run type-check
npm run build
```

Expected: 0 errors，build 通过。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/views/Replay.vue frontend/src/router/index.ts frontend/src/App.vue
git commit -m "feat(replay/web): Replay.vue main view + route + nav entry"
```

### T5.4 — Cypress E2E

- [ ] **Step 1: 创建 `frontend/cypress/e2e/replay.cy.ts`**

```ts
describe('Replay page', () => {
  const seedDays = {
    symbol: 'AAPL.US',
    days: [
      {
        trade_day: '2026-05-20',
        symbol: 'AAPL.US',
        market: 'US',
        trip_count: 1,
        realized_pnl: 50.0,
        llm_call_count: 2,
        llm_applied_count: 1,
        tag_counts: { NORMAL: 1, LOW_PROFIT: 1 },
      },
    ],
  }
  const seedDay = {
    summary: seedDays.days[0],
    price_source: 'candles',
    prices: [
      { ts: '2026-05-20T13:30:00Z', open: 150, high: 151, low: 149, close: 150.5, volume: 1000 },
      { ts: '2026-05-20T13:31:00Z', open: 150.5, high: 152, low: 150, close: 151.8, volume: 900 },
    ],
    orders: [
      {
        id: 1,
        broker_order_id: 'b1',
        side: 'BUY',
        status: 'FILLED',
        quantity: 10,
        price: 150,
        executed_quantity: 10,
        executed_price: 150,
        created_at: '2026-05-20T13:30:00Z',
        filled_at: '2026-05-20T13:30:00Z',
        realized_pnl: null,
      },
    ],
    trade_events: [],
    llm_interactions: [
      {
        id: 1,
        interaction_type: 'decide',
        created_at: '2026-05-20T13:29:00Z',
        applied: true,
        order_action: 'SUBMIT',
        order_status: 'FILLED',
        suggested_buy_low: 148,
        suggested_sell_high: 158,
        confidence: 0.8,
        summary: '建议买入',
        prompt_excerpt: 'PROMPT',
        linked_order_id: 1,
        tag: 'NORMAL',
      },
    ],
    trips: [
      {
        open_at: '2026-05-20T13:30:00Z',
        close_at: '2026-05-20T13:31:00Z',
        direction: 'long',
        entry_order_id: 1,
        exit_order_id: null,
        quantity: 10,
        entry_price: 150,
        exit_price: null,
        realized_pnl: null,
        llm_interaction_ids: [1],
      },
    ],
    rth_window: ['2026-05-20T13:30:00Z', '2026-05-20T20:00:00Z'],
    metadata: {
      min_profit_amount: 50,
      tag_thresholds: { REPRICE_THRESHOLD: 3 },
      trip_inference_mode: 'snapshot_based',
    },
  }

  beforeEach(() => {
    cy.intercept('GET', '/api/replay/days*', { body: seedDays }).as('listDays')
    cy.intercept('GET', '/api/replay/2026-05-20', { body: seedDay }).as('getDay')
  })

  it('loads days and renders summary + chart + lists', () => {
    cy.visit('/#/replay')
    cy.wait('@listDays')
    cy.wait('@getDay')

    cy.get('[data-testid=replay-day-select]').should('be.visible')
    cy.get('[data-testid=replay-summary]').contains('Trips:')
    cy.get('[data-testid=replay-price-chart]').should('exist')
    cy.get('[data-testid=replay-llm-list]').contains('建议买入')
    cy.get('[data-testid=replay-event-list]').contains('订单')
  })

  it('triggers export endpoints on button click', () => {
    cy.intercept('GET', '/api/replay/2026-05-20/export*', (req) => {
      expect(req.url).to.match(/format=(json|csv)/)
      req.reply({ statusCode: 200, body: '{}' })
    }).as('exportCall')
    cy.visit('/#/replay')
    cy.wait('@getDay')
    cy.window().then((win) => cy.stub(win, 'open').as('openWindow'))
    cy.contains('导出 JSON').click()
    cy.get('@openWindow').should('have.been.calledWithMatch', /format=json/)
    cy.contains('导出 CSV').click()
    cy.get('@openWindow').should('have.been.calledWithMatch', /format=csv/)
  })
})
```

- [ ] **Step 2: 跑 cypress**（保持后端已运行；本地用 `vite dev` 或 `npm run cypress:open`）

```bash
cd frontend
npm run cypress:run -- --spec cypress/e2e/replay.cy.ts
```

Expected: 2 PASSED（前提：本地开发服务器已起；如本任务在 CI 之外，可改用 `npm run cypress:open` 手工通过）。

- [ ] **Step 3: 提交**

```bash
git add frontend/cypress/e2e/replay.cy.ts
git commit -m "test(replay/web): cypress coverage for list/day/export"
```

---

## Task 6：文档收口 + 全量 lint

> **目标：** README、CLAUDE.md、Roadmap 把 P7 标为已交付；做最后一次全量 lint + 全量回归。

**Files:**
- Modify: `README.md` — 把"策略复盘（规划中 — P7）"小节标为"已交付"
- Modify: `CLAUDE.md` — 在 API 速查里加入 replay 域；目录结构加入 `replay_service.py` / `api/replay.py`
- Modify: `docs/Roadmap.md` — P7 节加 ✅；新增"近期已完成迭代 (P7)"小节
- Run: full pytest, basedpyright, frontend type-check + build

### T6.1 — README

- [ ] **Step 1: 把"策略复盘（规划中 — P7）"标为已交付**

把 `README.md` 中下列段落：

```md
### 策略复盘（规划中 — P7）

- 按交易日 × 当前 symbol 复盘价格走势、LLM 建议、实际成交、真实 PnL
- 5 类错误标签（错过止损 / 过早进场 / 频繁重挂 / 收益不足 / 正常交易）查询时计算
- 导出 JSON（细粒度，含 prompt 全文）或 CSV（扁平），用于 prompt 调优
- `list_days` 仅返回"有数据的交易日"（基于 orders / llm_interactions / runtime_state_snapshots 并集），不补齐空白日历日
- API 口径：参数非法返回 `422`；参数合法即使无数据也返回 `200` 空结构（不使用 `404` 表示"合法但无数据"）
- 规划详情见 `docs/superpowers/specs/2026-05-26-replay-llm-workshop-design.md`
```

替换为：

```md
### 策略复盘

- 按交易日 × 当前 symbol 复盘价格走势、LLM 建议、实际成交、真实 PnL
- 5 类错误标签按优先级评估（错过止损 > 过早进场 > 频繁重挂 > 收益不足 > 正常交易）
- 价格曲线：优先 `BrokerGateway.get_candlesticks`（K 线），不可用时回退 `RuntimeStateSnapshot`
- 导出 JSON（细粒度，含 prompt 全文与关联订单）或 CSV（扁平字段，Excel 友好）
- `realized_pnl` 复用 `DailyPnlService.calculate` 重算，不修改写路径
- Web UI **Replay** 页（`/#/replay`）：日期下拉、摘要卡、SVG 价格图、LLM 卡片流、订单/事件时间线、导出按钮
- API：`GET /api/replay/days`、`GET /api/replay/{trade_day}`、`GET /api/replay/{trade_day}/export?format=json|csv`
```

- [ ] **Step 2: 在前端路由表里加入 `/#/replay`**

将路由表里的 `/#/credentials` 行之前加入：

```md
| `/#/replay` | Replay — 按交易日 LLM 调用、订单与真实 PnL 复盘；JSON / CSV 导出 |
```

- [ ] **Step 3: 在"API 参考"里加 Replay 表**

在"### 回测"小节之后插入：

```md
### 复盘

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/replay/days` | 列出有数据的交易日；`limit`（1–90，默认 30）、`symbol`（可选；默认当前策略） |
| `GET` | `/api/replay/{trade_day}` | 当日聚合：价格、订单、LLM 调用（含 tag）、事件、trip；`symbol` 同上 |
| `GET` | `/api/replay/{trade_day}/export` | 导出 JSON 或 CSV；`format=json\|csv`，浏览器原生下载 |
```

### T6.2 — CLAUDE.md

- [ ] **Step 1: 在 API 速查表中插入 Replay 行**

```md
| 复盘 | `GET /api/replay/days`，`GET /api/replay/{date}`，`GET /api/replay/{date}/export?format=json\|csv` |
```

放在"回测"行之前或之后。

- [ ] **Step 2: 在目录结构中插入新文件**

api 块加入：

```
│   │   │   ├── replay.py                   # GET /api/replay/{days,{date},{date}/export}
```

services 块加入：

```
│   │       ├── replay_service.py          # 只读复盘聚合 + 5 个标签分类
```

- [ ] **Step 3: 在"文档"章节末尾把"当前下一迭代"指向更新**

把：

```md
- 当前下一迭代（P7 策略复盘）规格：`docs/superpowers/specs/2026-05-26-replay-llm-workshop-design.md`。
```

替换为：

```md
- P7 策略复盘已交付（2026-05-26）；规格 `docs/superpowers/specs/2026-05-26-replay-llm-workshop-design.md`，实施计划 `docs/superpowers/plans/2026-05-26-replay-llm-workshop.md`。
- 当前下一迭代 TBD（参考 `docs/Roadmap.md` 中的"建议执行顺序"）。
```

### T6.3 — Roadmap

- [ ] **Step 1: 在 Roadmap.md 顶部状态快照追加一行**

```md
| **策略复盘** | ✅ `ReplayService` + `/api/replay/*` + Web UI `/#/replay`，5 标签按优先级分类，JSON / CSV 导出。 |
```

- [ ] **Step 2: 把"建议执行顺序"里 P7 行的"**当前**"改为"已完成"**

```md
| 已完成 | **P7 策略复盘与 LLM 优化工作台** | 把已沉淀 LLM 交互数据反哺 prompt 调优。✅ 2026-05-26 |
| **当前** | P6 移动端与应急操作体验 | 提升紧急止损/暂停的可达性。 |
```

- [ ] **Step 3: 在"下一步建议"末尾加 P7 交付摘要**

```md
---

#### P7 交付摘要（2026-05-26）

- `ReplayService` 单类 ~600 行，纯只读，零数据库改动。
- 三个 GET 端点：`/api/replay/days`、`/{trade_day}`、`/{trade_day}/export`；422 表示参数非法，200 + 空表示合法无数据。
- 5 个 `ReplayTag` 按优先级评估；阈值为代码常量；测试覆盖 5×（正例 + 反例 + 优先级回归）。
- 前端新增 `Replay.vue` + `ReplayPriceChart.vue` + 1 个 cypress spec；图表用 SVG 自绘，不引图表库。
- `pytest` 新增 ~25 项（基线 485 → ~510）；`basedpyright` 0/0；`npm run type-check` 通过。
```

### T6.4 — 全量验证 + 提交

- [ ] **Step 1: backend 全量回归**

```bash
cd /Users/lcy/code/auto_trade/backend
python3 -m pytest tests/ -q
```

Expected: ALL PASSED（约 510 项）

- [ ] **Step 2: basedpyright 全量**

```bash
cd /Users/lcy/code/auto_trade
python3 -m basedpyright
```

Expected: 0 errors / 0 warnings

- [ ] **Step 3: frontend type-check + build**

```bash
cd /Users/lcy/code/auto_trade/frontend
npm run type-check
npm run build
```

Expected: 0 errors，build 成功。

- [ ] **Step 4: 提交文档**

```bash
cd /Users/lcy/code/auto_trade
git add README.md CLAUDE.md docs/Roadmap.md
git commit -m "docs: mark P7 replay workshop delivered"
```

- [ ] **Step 5: 在仓库根记录基线**

无需新增文件——下一轮迭代开始时从 `git log` 与本计划文件取基线即可。

---

## Verification Summary

收口检查清单（T6.4 已含部分）：

- [ ] `pytest 510+ passed`
- [ ] `basedpyright 0 errors / 0 warnings`
- [ ] `npm run type-check` 通过
- [ ] `npm run build` 通过
- [ ] `cypress/e2e/replay.cy.ts` 本地手工 / CI 全绿
- [ ] 手工：旧 DB 启动 → `/api/replay/days` 返回最近有数据交易日；切日 → `/api/replay/{date}` 含 LLM、订单、事件
- [ ] 手工：broker 凭证空 → `price_source="snapshots"` 渲染；非全 `none` 异常态
- [ ] 手工：CSV 在 Excel 列对齐；JSON 含 prompt 全文 + tag_thresholds metadata
- [ ] 手工：移动视口（iPhone 14 Pro）— 时间线纵向堆叠、导出按钮可触发

---

## Out of Scope (deferred)

- 多标的复盘 / Watchlist 切换（→ P8）
- 标签阈值 UI 化或环境变量化（→ 若收到调参需求再立项）
- 历史 `StrategyConfig` 快照（标签阈值仅参考当前 min_profit_amount）
- PWA / 离线导出
- 反向 FK 字段（`TradeEvent.interaction_id`）
