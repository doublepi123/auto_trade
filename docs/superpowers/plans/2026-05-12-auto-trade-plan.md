# Auto Trade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python+Vue automated trading system using Longbridge SDK for single-symbol range trading with Web UI configuration and real-time monitoring.

**Architecture:** FastAPI backend with SQLite persistence, Longbridge SDK for real-time quotes and trading, Vue 3 + Element Plus frontend. Strategy engine listens to Longbridge WebSocket quote pushes, evaluates price triggers against configured buy_low/sell_high thresholds, and executes orders through risk control.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, SQLAlchemy, SQLite, longport.openapi (Longbridge SDK), Vue 3, Vite, Element Plus, TypeScript, Docker Compose

---

### Task 1: Backend Project Skeleton

**Files:**
- Create: `backend/requirements.txt`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/app/config.py`
- Create: `backend/pytest.ini`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/test_config.py`

- [ ] **Step 1: Write requirements.txt**

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
sqlalchemy>=2.0.0
pydantic>=2.0.0
pydantic-settings>=2.0.0
longport>=0.1.0
httpx>=0.27.0
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

- [ ] **Step 2: Write `backend/app/__init__.py`**

```python
```

- [ ] **Step 3: Write `backend/app/config.py`**

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    env: str = "dev"
    database_url: str = "sqlite:///data/auto_trade.db"

    longbridge_app_key: str = ""
    longbridge_app_secret: str = ""
    longbridge_access_token: str = ""

    sct_key: str = ""

    default_strategy: dict = {
        "symbol": "",
        "market": "US",
        "buy_low": 0.0,
        "sell_high": 0.0,
        "short_selling": False,
    }

    default_risk: dict = {
        "max_daily_loss": 5000.0,
        "max_consecutive_losses": 3,
    }

    class Config:
        env_file = ".env"
        env_prefix = "AUTO_TRADE_"

    def ensure_data_dir(self) -> None:
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_data_dir()
```

- [ ] **Step 4: Write `backend/app/main.py`**

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    yield


app = FastAPI(title="Auto Trade", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "env": settings.env}
```

- [ ] **Step 5: Write `backend/pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
pythonpath = .
```

- [ ] **Step 6: Write `backend/tests/__init__.py`**

```python
```

- [ ] **Step 7: Write `backend/tests/test_config.py`**

```python
from app.config import Settings


class TestSettings:
    def test_default_values(self) -> None:
        s = Settings()
        assert s.env == "dev"
        assert s.database_url == "sqlite:///data/auto_trade.db"

    def test_default_strategy_empty(self) -> None:
        s = Settings()
        assert s.default_strategy["symbol"] == ""
        assert s.default_strategy["market"] == "US"

    def test_ensure_data_dir(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        data_dir = tmp / "data"
        original = Path.cwd()
        try:
            import os
            os.chdir(tmp)
            s = Settings(database_url=f"sqlite:///{tmp}/data/test.db")
            s.ensure_data_dir()
            assert data_dir.exists()
        finally:
            os.chdir(original)
            shutil.rmtree(tmp)
```

- [ ] **Step 8: Run tests to verify**

```bash
cd backend && pip install -r requirements.txt && python -m pytest tests/test_config.py -v
```

Expected: 3 tests pass.

- [ ] **Step 9: Commit**

```bash
git add backend/
git commit -m "feat: add backend project skeleton with config and tests"
```

---

### Task 2: Database Layer

**Files:**
- Create: `backend/app/database.py`
- Create: `backend/app/models.py`
- Create: `backend/tests/test_models.py`

- [ ] **Step 1: Write `backend/app/database.py`**

```python
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    from app.models import Base
    Base.metadata.create_all(bind=engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 2: Write `backend/app/models.py`**

```python
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StrategyConfig(Base):
    __tablename__ = "strategy_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), default="")
    market: Mapped[str] = mapped_column(String(10), default="US")
    buy_low: Mapped[float] = mapped_column(Float, default=0.0)
    sell_high: Mapped[float] = mapped_column(Float, default=0.0)
    short_selling: Mapped[bool] = mapped_column(Boolean, default=False)
    max_daily_loss: Mapped[float] = mapped_column(Float, default=5000.0)
    max_consecutive_losses: Mapped[int] = mapped_column(Integer, default=3)
    sct_key: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_order_id: Mapped[str] = mapped_column(String(100), default="")
    symbol: Mapped[str] = mapped_column(String(50))
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="SUBMITTED")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    raw_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RuntimeState(Base):
    __tablename__ = "runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engine_state: Mapped[str] = mapped_column(String(20), default="flat")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_trigger_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_trigger_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

- [ ] **Step 3: Write `backend/tests/test_models.py`**

```python
from app.database import init_db
from app.models import Base, StrategyConfig, OrderRecord, RiskEvent, RuntimeState


class TestModels:
    @classmethod
    def setup_class(cls) -> None:
        import os
        os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_models.db"
        init_db()

    def test_strategy_config_table_exists(self) -> None:
        assert StrategyConfig.__tablename__ == "strategy_config"

    def test_order_record_fields(self) -> None:
        o = OrderRecord(symbol="AAPL.US", side="BUY", quantity=10, price=150.0)
        assert o.symbol == "AAPL.US"
        assert o.side == "BUY"
        assert o.status == "SUBMITTED"

    def test_risk_event_fields(self) -> None:
        e = RiskEvent(event_type="DAILY_LOSS", reason="exceeded max daily loss")
        assert e.event_type == "DAILY_LOSS"

    def test_runtime_state_defaults(self) -> None:
        s = RuntimeState()
        assert s.engine_state == "flat"
        assert s.paused is False
        assert s.kill_switch is False
```

- [ ] **Step 4: Run tests to verify**

```bash
cd backend && python -m pytest tests/test_models.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/database.py backend/app/models.py backend/tests/test_models.py
git commit -m "feat: add database layer with SQLAlchemy models"
```

---

### Task 3: Pydantic Schemas

**Files:**
- Create: `backend/app/schemas.py`

- [ ] **Step 1: Write `backend/app/schemas.py`**

```python
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class StrategyConfigSchema(BaseModel):
    symbol: str = Field(default="", max_length=50)
    market: str = Field(default="US")
    buy_low: float = Field(default=0.0, gt=0)
    sell_high: float = Field(default=0.0, gt=0)
    short_selling: bool = Field(default=False)
    max_daily_loss: float = Field(default=5000.0, gt=0)
    max_consecutive_losses: int = Field(default=3, ge=1)
    sct_key: str = Field(default="", max_length=200)

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        if v not in ("US", "HK"):
            raise ValueError("market must be US or HK")
        return v

    @field_validator("sell_high")
    @classmethod
    def validate_sell_high(cls, v: float, info: any) -> float:
        buy_low = info.data.get("buy_low", 0.0)
        if v <= buy_low:
            raise ValueError("sell_high must be greater than buy_low")
        return v


class StrategyResponse(BaseModel):
    id: int
    symbol: str
    market: str
    buy_low: float
    sell_high: float
    short_selling: bool
    max_daily_loss: float
    max_consecutive_losses: int
    sct_key: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class StatusResponse(BaseModel):
    engine_state: str
    paused: bool
    kill_switch: bool
    daily_pnl: float
    consecutive_losses: int
    last_price: float
    last_trigger_price: float
    last_trigger_at: Optional[datetime]

    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    id: int
    broker_order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    status: str
    created_at: datetime
    filled_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ControlRequest(BaseModel):
    reason: str = Field(default="manual")


class MessageResponse(BaseModel):
    message: str
```

---

### Task 4: Longbridge Broker Wrapper

**Files:**
- Create: `backend/app/core/__init__.py`
- Create: `backend/app/core/broker.py`
- Create: `backend/tests/test_broker.py`

- [ ] **Step 1: Write `backend/app/core/__init__.py`**

```python
```

- [ ] **Step 2: Write `backend/app/core/broker.py`**

```python
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional


def _import_openapi() -> Any:
    for name in ("longport.openapi", "longbridge.openapi"):
        try:
            return __import__(name, fromlist=["Config"])
        except ModuleNotFoundError:
            continue
    raise RuntimeError("Longbridge SDK not installed. Install longport or longbridge.")


@dataclass
class Quote:
    symbol: str
    last_price: float
    bid: float
    ask: float
    timestamp: str


@dataclass
class OrderResult:
    broker_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    status: str


@dataclass
class Position:
    symbol: str
    side: str
    quantity: Decimal
    avg_price: Decimal


class BrokerGateway:
    def __init__(self) -> None:
        self._quote_ctx: Any = None
        self._trade_ctx: Any = None
        self._quote_callbacks: list[Callable[[Quote], None]] = []
        self._ws_running = False

    def _init_clients(self) -> None:
        if self._quote_ctx is None:
            module = _import_openapi()
            app_key = os.getenv("LONGBRIDGE_APP_KEY", "") or os.getenv("LONGPORT_APP_KEY", "")
            app_secret = os.getenv("LONGBRIDGE_APP_SECRET", "") or os.getenv("LONGPORT_APP_SECRET", "")
            access_token = os.getenv("LONGBRIDGE_ACCESS_TOKEN", "") or os.getenv("LONGPORT_ACCESS_TOKEN", "")

            config = module.Config.from_apikey(app_key, app_secret, access_token)
            self._quote_ctx = module.QuoteContext(config)
            self._trade_ctx = module.TradeContext(config)

    def get_quote(self, symbol: str) -> Quote:
        self._init_clients()
        response = self._quote_ctx.quote([symbol])
        items = response if isinstance(response, list) else [response]
        if not items:
            raise ValueError(f"no quote data for {symbol}")
        item = items[0]
        return Quote(
            symbol=str(getattr(item, "symbol", symbol)),
            last_price=float(getattr(item, "last_done", 0)),
            bid=float(getattr(item, "bid", 0)),
            ask=float(getattr(item, "ask", 0)),
            timestamp=str(getattr(item, "timestamp", "")),
        )

    def subscribe_quotes(self, symbol: str, callback: Callable[[Quote], None]) -> None:
        self._init_clients()
        self._quote_callbacks.append(callback)
        module = _import_openapi()
        TopicType = getattr(module, "TopicType", None)

        def _on_quote(_symbol: str, _event: Any) -> None:
            quote = Quote(
                symbol=str(getattr(_event, "symbol", _symbol)),
                last_price=float(getattr(_event, "last_done", 0)),
                bid=float(getattr(_event, "bid", 0)),
                ask=float(getattr(_event, "ask", 0)),
                timestamp=str(getattr(_event, "timestamp", "")),
            )
            for cb in self._quote_callbacks:
                cb(quote)

        self._quote_ctx.set_on_quote(_on_quote)
        topics = [TopicType.Quote] if TopicType else []
        self._quote_ctx.subscribe([symbol], topics)

    def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
        self._init_clients()
        module = _import_openapi()
        OrderSide = getattr(module, "OrderSide", None)
        OrderType = getattr(module, "OrderType", None)
        TimeInForceType = getattr(module, "TimeInForceType", None)

        side_enum = getattr(OrderSide, side.capitalize()) if OrderSide else side
        lo_type = getattr(OrderType, "LO") if OrderType else "LO"
        day_tif = getattr(TimeInForceType, "Day") if TimeInForceType else "DAY"

        response = self._trade_ctx.submit_order(
            symbol=symbol,
            order_type=lo_type,
            side=side_enum,
            submitted_quantity=quantity,
            time_in_force=day_tif,
            submitted_price=price,
            remark="auto-trade",
        )

        order_id = str(getattr(response, "order_id", getattr(response, "broker_order_id", str(response))))
        return OrderResult(
            broker_order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            status="SUBMITTED",
        )

    def get_positions(self) -> list[Position]:
        self._init_clients()
        response = self._trade_ctx.stock_positions()
        items = response if isinstance(response, list) else getattr(response, "channels", [response])
        positions: list[Position] = []
        for item in items:
            qty = Decimal(str(getattr(item, "available_quantity", getattr(item, "quantity", "0"))))
            if qty > 0:
                positions.append(Position(
                    symbol=str(getattr(item, "symbol", "")),
                    side="LONG",
                    quantity=qty,
                    avg_price=Decimal(str(getattr(item, "cost_price", "0"))),
                ))
        return positions

    def get_cash(self) -> Decimal:
        self._init_clients()
        try:
            response = self._trade_ctx.account_balance()
            if isinstance(response, list) and response:
                for item in response:
                    currency = getattr(item, "currency", "")
                    if currency in ("USD", "HKD"):
                        return Decimal(str(getattr(item, "available_cash", getattr(item, "cash", "0"))))
                return Decimal(str(getattr(response[0], "available_cash", "0")))
            return Decimal(str(getattr(response, "available_cash", getattr(response, "cash", "0"))))
        except Exception:
            return Decimal("0")
```

- [ ] **Step 3: Write `backend/tests/test_broker.py`**

```python
from unittest.mock import MagicMock, patch

from app.core.broker import BrokerGateway, Quote, _import_openapi


class TestQuote:
    def test_quote_dataclass(self) -> None:
        q = Quote(symbol="AAPL.US", last_price=150.0, bid=149.5, ask=150.5, timestamp="2026-01-01")
        assert q.symbol == "AAPL.US"
        assert q.last_price == 150.0
        assert q.bid == 149.5
        assert q.ask == 150.5

    def test_quote_defaults(self) -> None:
        q = Quote(symbol="TSLA.US", last_price=0, bid=0, ask=0, timestamp="")
        assert q.last_price == 0


class TestBrokerGateway:
    def test_init_no_credentials(self) -> None:
        gw = BrokerGateway()
        assert gw._quote_ctx is None
        assert gw._trade_ctx is None

    def test_quote_callbacks_registration(self) -> None:
        gw = BrokerGateway()
        received: list[Quote] = []

        def cb(q: Quote) -> None:
            received.append(q)

        gw._quote_callbacks.append(cb)
        assert len(gw._quote_callbacks) == 1

        test_q = Quote(symbol="NVDA.US", last_price=120.0, bid=119.5, ask=120.5, timestamp="")
        gw._quote_callbacks[0](test_q)
        assert len(received) == 1
        assert received[0].last_price == 120.0
```

- [ ] **Step 4: Run tests to verify**

```bash
cd backend && python -m pytest tests/test_broker.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/__init__.py backend/app/core/broker.py backend/tests/test_broker.py
git commit -m "feat: add Longbridge broker wrapper with quote and trade"
```

---

### Task 5: Notification Module

**Files:**
- Create: `backend/app/core/notify.py`
- Create: `backend/tests/test_notify.py`

- [ ] **Step 1: Write `backend/app/core/notify.py`**

```python
from __future__ import annotations

import httpx


class ServerChanNotifier:
    def __init__(self, sct_key: str) -> None:
        self._sct_key = sct_key
        self._url = f"https://sctapi.ftqq.com/{sct_key}.send"

    def send(self, title: str, content: str = "") -> bool:
        if not self._sct_key:
            return False
        try:
            resp = httpx.post(self._url, data={"title": title, "desp": content}, timeout=10)
            return resp.status_code == 200
        except Exception:
            return False

    def notify_order(self, side: str, symbol: str, quantity: str, price: str, order_id: str) -> bool:
        title = f"[Auto Trade] {side} Order Submitted"
        content = f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}\nOrder ID: {order_id}"
        return self.send(title, content)

    def notify_risk_event(self, event_type: str, reason: str) -> bool:
        title = f"[Auto Trade] Risk Event: {event_type}"
        content = f"Type: {event_type}\nReason: {reason}"
        return self.send(title, content)

    def notify_fill(self, symbol: str, side: str, quantity: str, price: str) -> bool:
        title = f"[Auto Trade] Order Filled"
        content = f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}"
        return self.send(title, content)
```

- [ ] **Step 2: Write `backend/tests/test_notify.py`**

```python
from unittest.mock import patch, MagicMock

from app.core.notify import ServerChanNotifier


class TestServerChanNotifier:
    def test_send_without_key(self) -> None:
        notifier = ServerChanNotifier("")
        assert notifier.send("test", "content") is False

    @patch("app.core.notify.httpx")
    def test_send_success(self, mock_httpx: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.post.return_value = mock_resp

        notifier = ServerChanNotifier("test_key")
        result = notifier.send("hello", "world")

        assert result is True
        mock_httpx.post.assert_called_once()

    @patch("app.core.notify.httpx")
    def test_notify_order(self, mock_httpx: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.post.return_value = mock_resp

        notifier = ServerChanNotifier("test_key")
        result = notifier.notify_order("BUY", "AAPL.US", "10", "150.0", "order-123")

        assert result is True
        call_args = mock_httpx.post.call_args[1]["data"]
        assert "[Auto Trade] BUY" in call_args["title"]

    @patch("app.core.notify.httpx")
    def test_notify_risk_event(self, mock_httpx: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.post.return_value = mock_resp

        notifier = ServerChanNotifier("test_key")
        result = notifier.notify_risk_event("DAILY_LOSS", "exceeded limit")

        assert result is True
        call_args = mock_httpx.post.call_args[1]["data"]
        assert "DAILY_LOSS" in call_args["title"]
```

- [ ] **Step 3: Run tests to verify**

```bash
cd backend && python -m pytest tests/test_notify.py -v
```

Expected: 4 tests pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/core/notify.py backend/tests/test_notify.py
git commit -m "feat: add ServerChan notification module"
```

---

### Task 6: Risk Control Module

**Files:**
- Create: `backend/app/core/risk.py`
- Create: `backend/tests/test_risk.py`

- [ ] **Step 1: Write `backend/app/core/risk.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class RiskConfig:
    max_daily_loss: float = 5000.0
    max_consecutive_losses: int = 3


@dataclass
class RiskResult:
    approved: bool
    reason: str = ""


class RiskController:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self.daily_pnl: float = 0.0
        self._today: date = date.today()
        self.consecutive_losses: int = 0
        self.kill_switch: bool = False
        self.paused: bool = False

    def check(self) -> RiskResult:
        if self.kill_switch:
            return RiskResult(approved=False, reason="kill switch is active")
        if self.paused:
            return RiskResult(approved=False, reason="trading is paused")
        return self._check_limits()

    def _check_limits(self) -> RiskResult:
        today = date.today()
        if today != self._today:
            self.daily_pnl = 0.0
            self._today = today

        if self.daily_pnl <= -abs(self.config.max_daily_loss):
            return RiskResult(approved=False, reason=f"daily loss limit reached: {self.daily_pnl}")

        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return RiskResult(approved=False, reason=f"max consecutive losses reached: {self.consecutive_losses}")

        return RiskResult(approved=True)

    def record_trade(self, pnl: float) -> None:
        today = date.today()
        if today != self._today:
            self.daily_pnl = 0.0
            self._today = today

        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def pause(self, reason: str = "manual") -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def enable_kill_switch(self, reason: str = "manual") -> None:
        self.kill_switch = True

    def disable_kill_switch(self) -> None:
        self.kill_switch = False
```

- [ ] **Step 2: Write `backend/tests/test_risk.py`**

```python
from app.core.risk import RiskConfig, RiskController


class TestRiskController:
    def test_default_approved(self) -> None:
        ctrl = RiskController()
        result = ctrl.check()
        assert result.approved is True

    def test_paused_rejected(self) -> None:
        ctrl = RiskController()
        ctrl.pause()
        result = ctrl.check()
        assert result.approved is False
        assert "paused" in result.reason.lower()

    def test_resume_after_pause(self) -> None:
        ctrl = RiskController()
        ctrl.pause()
        ctrl.resume()
        result = ctrl.check()
        assert result.approved is True

    def test_kill_switch_rejected(self) -> None:
        ctrl = RiskController()
        ctrl.enable_kill_switch()
        result = ctrl.check()
        assert result.approved is False
        assert "kill" in result.reason.lower()

    def test_daily_loss_limit_reached(self) -> None:
        config = RiskConfig(max_daily_loss=100.0, max_consecutive_losses=3)
        ctrl = RiskController(config)
        ctrl.record_trade(-100.0)
        result = ctrl.check()
        assert result.approved is False
        assert "daily loss" in result.reason.lower()

    def test_daily_loss_not_reached(self) -> None:
        config = RiskConfig(max_daily_loss=100.0, max_consecutive_losses=3)
        ctrl = RiskController(config)
        ctrl.record_trade(-50.0)
        result = ctrl.check()
        assert result.approved is True

    def test_consecutive_losses_reached(self) -> None:
        config = RiskConfig(max_daily_loss=5000.0, max_consecutive_losses=2)
        ctrl = RiskController(config)
        ctrl.record_trade(-10.0)
        ctrl.record_trade(-10.0)
        result = ctrl.check()
        assert result.approved is False
        assert "consecutive" in result.reason.lower()

    def test_winning_resets_consecutive(self) -> None:
        config = RiskConfig(max_daily_loss=5000.0, max_consecutive_losses=3)
        ctrl = RiskController(config)
        ctrl.record_trade(-10.0)
        ctrl.record_trade(5.0)
        assert ctrl.consecutive_losses == 0
        result = ctrl.check()
        assert result.approved is True
```

- [ ] **Step 3: Run tests to verify**

```bash
cd backend && python -m pytest tests/test_risk.py -v
```

Expected: 8 tests pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/core/risk.py backend/tests/test_risk.py
git commit -m "feat: add risk control module"
```

---

### Task 7: Strategy Engine (State Machine)

**Files:**
- Create: `backend/app/core/engine.py`
- Create: `backend/tests/test_engine.py`

- [ ] **Step 1: Write `backend/app/core/engine.py`**

```python
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("auto_trade.engine")


class EngineState(str, Enum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


@dataclass
class StrategyParams:
    symbol: str = ""
    market: str = "US"
    buy_low: float = 0.0
    sell_high: float = 0.0
    short_selling: bool = False


@dataclass
class TriggerResult:
    triggered: bool
    action: str = ""
    description: str = ""


class StrategyEngine:
    def __init__(self, params: StrategyParams | None = None) -> None:
        self.params = params or StrategyParams()
        self.state: EngineState = EngineState.FLAT
        self.last_price: float = 0.0
        self.last_trigger_price: float = 0.0
        self.last_trigger_at: Optional[datetime] = None
        self._cooldown_seconds: int = 60

    def update_price(self, price: float) -> TriggerResult:
        self.last_price = price

        if not self.params.symbol or self.params.buy_low <= 0 or self.params.sell_high <= 0:
            return TriggerResult(triggered=False)

        if self._in_cooldown():
            return TriggerResult(triggered=False)

        if self.state == EngineState.FLAT:
            if price <= self.params.buy_low:
                self.state = EngineState.LONG
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="BUY",
                    description=f"Price {price} <= buy_low {self.params.buy_low}, go LONG",
                )
            if self.params.short_selling and price >= self.params.sell_high:
                self.state = EngineState.SHORT
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="SELL_SHORT",
                    description=f"Price {price} >= sell_high {self.params.sell_high}, go SHORT",
                )

        elif self.state == EngineState.LONG:
            if price >= self.params.sell_high:
                self.state = EngineState.FLAT
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="SELL",
                    description=f"Price {price} >= sell_high {self.params.sell_high}, sell LONG",
                )

        elif self.state == EngineState.SHORT:
            if price <= self.params.buy_low:
                self.state = EngineState.FLAT
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="BUY_TO_COVER",
                    description=f"Price {price} <= buy_low {self.params.buy_low}, cover SHORT",
                )

        return TriggerResult(triggered=False)

    def _mark_trigger(self, price: float) -> None:
        self.last_trigger_price = price
        self.last_trigger_at = datetime.utcnow()

    def _in_cooldown(self) -> bool:
        if self.last_trigger_at is None:
            return False
        elapsed = (datetime.utcnow() - self.last_trigger_at).total_seconds()
        return elapsed < self._cooldown_seconds

    def sync_state(self, has_long_position: bool, has_short_position: bool) -> None:
        if has_long_position:
            self.state = EngineState.LONG
        elif has_short_position:
            self.state = EngineState.SHORT
        else:
            self.state = EngineState.FLAT

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "last_price": self.last_price,
            "last_trigger_price": self.last_trigger_price,
            "last_trigger_at": self.last_trigger_at.isoformat() if self.last_trigger_at else None,
            "symbol": self.params.symbol,
            "buy_low": self.params.buy_low,
            "sell_high": self.params.sell_high,
            "short_selling": self.params.short_selling,
        }
```

- [ ] **Step 2: Write `backend/tests/test_engine.py`**

```python
from app.core.engine import EngineState, StrategyEngine, StrategyParams, TriggerResult


def make_params(buy_low: float = 100.0, sell_high: float = 200.0, short_selling: bool = False) -> StrategyParams:
    return StrategyParams(symbol="AAPL.US", market="US", buy_low=buy_low, sell_high=sell_high, short_selling=short_selling)


class TestStrategyEngine:
    def test_default_state_is_flat(self) -> None:
        engine = StrategyEngine()
        assert engine.state == EngineState.FLAT

    def test_price_below_buy_low_from_flat_triggers_buy(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        result = engine.update_price(99.0)
        assert result.triggered is True
        assert result.action == "BUY"
        assert engine.state == EngineState.LONG

    def test_price_above_sell_high_from_long_triggers_sell(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        result = engine.update_price(201.0)
        assert result.triggered is True
        assert result.action == "SELL"
        assert engine.state == EngineState.FLAT

    def test_price_range_no_trigger_from_flat(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        result = engine.update_price(150.0)
        assert result.triggered is False
        assert engine.state == EngineState.FLAT

    def test_price_range_no_trigger_from_long(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        result = engine.update_price(150.0)
        assert result.triggered is False
        assert engine.state == EngineState.LONG

    def test_short_selling_enabled_triggers_short(self) -> None:
        engine = StrategyEngine(make_params(100, 200, short_selling=True))
        result = engine.update_price(201.0)
        assert result.triggered is True
        assert result.action == "SELL_SHORT"
        assert engine.state == EngineState.SHORT

    def test_short_selling_disabled_does_not_trigger_short(self) -> None:
        engine = StrategyEngine(make_params(100, 200, short_selling=False))
        result = engine.update_price(201.0)
        assert result.triggered is False
        assert engine.state == EngineState.FLAT

    def test_cover_short_when_below_buy_low(self) -> None:
        engine = StrategyEngine(make_params(100, 200, short_selling=True))
        engine.state = EngineState.SHORT
        result = engine.update_price(99.0)
        assert result.triggered is True
        assert result.action == "BUY_TO_COVER"
        assert engine.state == EngineState.FLAT

    def test_no_trigger_when_params_empty(self) -> None:
        engine = StrategyEngine(StrategyParams())
        result = engine.update_price(50.0)
        assert result.triggered is False

    def test_sync_state_from_positions(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.sync_state(has_long_position=True, has_short_position=False)
        assert engine.state == EngineState.LONG

        engine.sync_state(has_long_position=False, has_short_position=True)
        assert engine.state == EngineState.SHORT

        engine.sync_state(has_long_position=False, has_short_position=False)
        assert engine.state == EngineState.FLAT

    def test_to_dict(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        d = engine.to_dict()
        assert d["state"] == "flat"
        assert d["symbol"] == "AAPL.US"
        assert d["buy_low"] == 100.0
        assert d["sell_high"] == 200.0
```

- [ ] **Step 3: Run tests to verify**

```bash
cd backend && python -m pytest tests/test_engine.py -v
```

Expected: 11 tests pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/core/engine.py backend/tests/test_engine.py
git commit -m "feat: add strategy engine with state machine"
```

---

### Task 8: Strategy Service (Business Logic Layer)

**Files:**
- Create: `backend/app/services/__init__.py`
- Create: `backend/app/services/strategy_service.py`
- Create: `backend/tests/test_strategy_service.py`

- [ ] **Step 1: Write `backend/app/services/__init__.py`**

```python
```

- [ ] **Step 2: Write `backend/app/services/strategy_service.py`**

```python
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import RuntimeState, StrategyConfig


class StrategyService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_config(self) -> StrategyConfig:
        config = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        if config is None:
            config = StrategyConfig()
            self.db.add(config)
            self.db.commit()
        return config

    def update_config(self, data: dict) -> StrategyConfig:
        config = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        if config is None:
            config = StrategyConfig()

        updatable_fields = [
            "symbol", "market", "buy_low", "sell_high",
            "short_selling", "max_daily_loss", "max_consecutive_losses", "sct_key",
        ]
        for field in updatable_fields:
            if field in data:
                setattr(config, field, data[field])

        config.updated_at = datetime.utcnow()
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        return config

    def get_runtime_state(self) -> RuntimeState:
        state = self.db.query(RuntimeState).order_by(RuntimeState.id.desc()).first()
        if state is None:
            state = RuntimeState()
            self.db.add(state)
            self.db.commit()
        return state

    def update_runtime_state(self, **kwargs: object) -> RuntimeState:
        state = self.get_runtime_state()
        for key, value in kwargs.items():
            if hasattr(state, key):
                setattr(state, key, value)
        state.updated_at = datetime.utcnow()
        self.db.add(state)
        self.db.commit()
        self.db.refresh(state)
        return state
```

- [ ] **Step 3: Write `backend/tests/test_strategy_service.py`**

```python
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import StrategyConfig, RuntimeState
from app.services.strategy_service import StrategyService


DB_URL = "sqlite:///data/test_service.db"


class TestStrategyService:
    @classmethod
    def setup_class(cls) -> None:
        engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        cls.engine = engine

    def _get_db(self) -> Session:
        return Session(bind=self.engine)

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(StrategyConfig).delete()
        db.query(RuntimeState).delete()
        db.commit()
        db.close()

    def test_get_config_creates_default(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        config = svc.get_config()
        assert config is not None
        assert config.symbol == ""
        assert config.market == "US"
        db.close()

    def test_update_config(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        updated = svc.update_config({
            "symbol": "AAPL.US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert updated.symbol == "AAPL.US"
        assert updated.buy_low == 100.0
        assert updated.sell_high == 200.0

        config = svc.get_config()
        assert config.symbol == "AAPL.US"
        db.close()

    def test_get_runtime_state_defaults(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        state = svc.get_runtime_state()
        assert state.engine_state == "flat"
        assert state.paused is False
        db.close()

    def test_update_runtime_state(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        updated = svc.update_runtime_state(engine_state="long", last_price=150.0)
        assert updated.engine_state == "long"
        assert updated.last_price == 150.0
        db.close()
```

- [ ] **Step 4: Run tests to verify**

```bash
cd backend && python -m pytest tests/test_strategy_service.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ backend/tests/test_strategy_service.py
git commit -m "feat: add strategy service for config and state management"
```

---

### Task 9: API Endpoints

**Files:**
- Create: `backend/app/api/__init__.py`
- Create: `backend/app/api/strategy.py`
- Create: `backend/app/api/trade.py`
- Create: `backend/app/api/ws.py`
- Modify: `backend/app/main.py` (register routers)

- [ ] **Step 1: Write `backend/app/api/__init__.py`**

```python
```

- [ ] **Step 2: Write `backend/app/api/strategy.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import ControlRequest, MessageResponse, StatusResponse, StrategyConfigSchema, StrategyResponse
from app.services.strategy_service import StrategyService

router = APIRouter(prefix="/api", tags=["strategy"])


@router.get("/strategy", response_model=StrategyResponse)
def get_strategy(db: Session = Depends(get_db)) -> StrategyResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    return StrategyResponse.model_validate(config)


@router.put("/strategy", response_model=StrategyResponse)
def update_strategy(payload: StrategyConfigSchema, db: Session = Depends(get_db)) -> StrategyResponse:
    svc = StrategyService(db)
    config = svc.update_config(payload.model_dump(exclude_unset=True))
    return StrategyResponse.model_validate(config)


@router.get("/status", response_model=StatusResponse)
def get_status(db: Session = Depends(get_db)) -> StatusResponse:
    svc = StrategyService(db)
    state = svc.get_runtime_state()
    return StatusResponse.model_validate(state)
```

- [ ] **Step 3: Write `backend/app/api/trade.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import OrderRecord
from app.runner import get_runner
from app.schemas import ControlRequest, MessageResponse, OrderResponse
from app.services.strategy_service import StrategyService

router = APIRouter(prefix="/api", tags=["trade"])


@router.get("/orders", response_model=list[OrderResponse])
def get_orders(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[OrderResponse]:
    orders = db.query(OrderRecord).order_by(OrderRecord.created_at.desc()).limit(limit).all()
    return [OrderResponse.model_validate(o) for o in orders]


@router.post("/control/start", response_model=MessageResponse)
def start_runner(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=False, kill_switch=False)
    get_runner().start()
    return MessageResponse(message="runner started")


@router.post("/control/stop", response_model=MessageResponse)
def stop_runner(payload: ControlRequest, db: Session = Depends(get_db)) -> MessageResponse:
    get_runner().stop()
    svc = StrategyService(db)
    svc.update_runtime_state(paused=True)
    return MessageResponse(message="runner stopped")


@router.post("/control/pause", response_model=MessageResponse)
def pause_trading(
    payload: ControlRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=True)
    return MessageResponse(message="trading paused")


@router.post("/control/resume", response_model=MessageResponse)
def resume_trading(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=False)
    return MessageResponse(message="trading resumed")


@router.post("/control/kill-switch", response_model=MessageResponse)
def kill_switch(
    payload: ControlRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(kill_switch=True)
    return MessageResponse(message="kill switch activated")
```

- [ ] **Step 4: Write `backend/app/api/ws.py`**

```python
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("auto_trade.ws")

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active_connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for conn in self.active_connections:
            try:
                await conn.send_text(json.dumps(message))
            except Exception:
                dead.append(conn)
        for conn in dead:
            self.disconnect(conn)


manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(ws)
```

- [ ] **Step 5: Update `backend/app/main.py`**

Read: `backend/app/main.py`
Replace the entire file content:

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.strategy import router as strategy_router
from app.api.trade import router as trade_router
from app.api.ws import router as ws_router
from app.config import settings
from app.database import init_db
from app.runner import get_runner


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    get_runner().start()
    yield
    get_runner().stop()


app = FastAPI(title="Auto Trade", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(strategy_router)
app.include_router(trade_router)
app.include_router(ws_router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "env": settings.env}
```

- [ ] **Step 6: Write `backend/tests/test_api.py`**

```python
import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_api.db"

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


class TestAPI:
    def test_health(self) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_get_strategy_default(self) -> None:
        resp = client.get("/api/strategy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == ""

    def test_update_strategy_valid(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "short_selling": False,
            "max_daily_loss": 5000.0,
            "max_consecutive_losses": 3,
            "sct_key": "",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "AAPL.US"
        assert data["buy_low"] == 100.0
        assert data["sell_high"] == 200.0

    def test_update_strategy_invalid_market(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "CN",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert resp.status_code == 422

    def test_update_strategy_sell_lt_buy(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 200.0,
            "sell_high": 100.0,
        })
        assert resp.status_code == 422

    def test_get_status(self) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "engine_state" in data
        assert data["engine_state"] == "flat"

    def test_pause_trading(self) -> None:
        resp = client.post("/api/control/pause", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "trading paused"

    def test_resume_trading(self) -> None:
        resp = client.post("/api/control/resume")
        assert resp.status_code == 200
        assert resp.json()["message"] == "trading resumed"

    def test_kill_switch(self) -> None:
        resp = client.post("/api/control/kill-switch", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "kill switch activated"

    def test_start_runner(self) -> None:
        resp = client.post("/api/control/start")
        assert resp.status_code == 200
        assert resp.json()["message"] == "runner started"

    def test_stop_runner(self) -> None:
        resp = client.post("/api/control/stop", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "runner stopped"

    def test_get_orders_empty(self) -> None:
        resp = client.get("/api/orders")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
```

- [ ] **Step 7: Run tests to verify**

```bash
cd backend && python -m pytest tests/test_api.py -v
```

Expected: 14 tests pass.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/ backend/app/main.py backend/tests/test_api.py
git commit -m "feat: add REST API endpoints for strategy, status, and control"
```

---

### Task 10: Application Runner (Main Process)

**Files:**
- Create: `backend/app/runner.py`
- Create: `backend/docker-entrypoint.sh`

- [ ] **Step 1: Write `backend/app/runner.py`**

```python
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import settings
from app.core.broker import BrokerGateway, Quote
from app.core.engine import StrategyEngine, StrategyParams, TriggerResult
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal, init_db
from app.models import OrderRecord, RiskEvent
from app.services.strategy_service import StrategyService
from app.api.ws import manager

logger = logging.getLogger("auto_trade.runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


class AppRunner:
    def __init__(self) -> None:
        self.broker = BrokerGateway()
        self.engine = StrategyEngine()
        self.risk = RiskController()
        self.notifier = ServerChanNotifier("")
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        db = SessionLocal()
        try:
            svc = StrategyService(db)
            config = svc.get_config()
            state = svc.get_runtime_state()

            self.engine.params = StrategyParams(
                symbol=config.symbol,
                market=config.market,
                buy_low=config.buy_low,
                sell_high=config.sell_high,
                short_selling=config.short_selling,
            )
            self.engine.state = state.engine_state
            self.engine.last_price = state.last_price

            self.risk.config = RiskConfig(
                max_daily_loss=config.max_daily_loss,
                max_consecutive_losses=config.max_consecutive_losses,
            )
            self.risk.daily_pnl = state.daily_pnl
            self.risk.consecutive_losses = state.consecutive_losses
            self.risk.kill_switch = state.kill_switch
            self.risk.paused = state.paused

            self.notifier = ServerChanNotifier(config.sct_key)

            if config.symbol:
                self.broker.subscribe_quotes(config.symbol, self._on_quote)
                logger.info(f"subscribed to {config.symbol} quotes")
        finally:
            db.close()

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("runner started")

    def stop(self) -> None:
        self._running = False

    def _on_quote(self, quote: Quote) -> None:
        try:
            result = self.engine.update_price(quote.last_price)
            self._broadcast_status()

            if result.triggered:
                self._handle_trigger(result, quote)
        except Exception:
            logger.exception("error processing quote")

    def _handle_trigger(self, result: TriggerResult, quote: Quote) -> None:
        risk_result = self.risk.check()
        if not risk_result.approved:
            logger.warning(f"risk rejected: {risk_result.reason}")
            self._record_risk_event(risk_result.reason)
            self.notifier.notify_risk_event("REJECTED", risk_result.reason)
            return

        try:
            symbol = self.engine.params.symbol
            if result.action == "BUY":
                self._execute_buy(symbol, quote)
            elif result.action == "SELL":
                self._execute_sell(symbol, quote)
            elif result.action == "SELL_SHORT":
                self._execute_sell_short(symbol, quote)
            elif result.action == "BUY_TO_COVER":
                self._execute_buy_to_cover(symbol, quote)

            self._broadcast_status()
        except Exception as exc:
            logger.exception(f"order execution failed: {exc}")
            self._record_risk_event(str(exc))
            self.notifier.notify_risk_event("ORDER_FAILED", str(exc))

    def _execute_buy(self, symbol: str, quote: Quote) -> None:
        cash = self.broker.get_cash()
        price = Decimal(str(quote.last_price))
        qty = (cash / price).quantize(Decimal("0.01"))
        if qty <= 0:
            return

        result = self.broker.submit_limit_order(symbol, "BUY", qty, price)
        self._record_order(result.broker_order_id, symbol, "BUY", float(qty), float(price))
        self.notifier.notify_order("BUY", symbol, str(qty), str(price), result.broker_order_id)
        logger.info(f"BUY: {symbol} qty={qty} price={price}")

    def _execute_sell(self, symbol: str, quote: Quote) -> None:
        positions = self.broker.get_positions()
        long_pos = next((p for p in positions if p.symbol == symbol and p.side == "LONG"), None)
        if long_pos is None:
            return

        price = Decimal(str(quote.last_price))
        result = self.broker.submit_limit_order(symbol, "SELL", long_pos.quantity, price)
        self._record_order(result.broker_order_id, symbol, "SELL", float(long_pos.quantity), float(price))
        self.notifier.notify_order("SELL", symbol, str(long_pos.quantity), str(price), result.broker_order_id)
        logger.info(f"SELL: {symbol} qty={long_pos.quantity} price={price}")

    def _execute_sell_short(self, symbol: str, quote: Quote) -> None:
        cash = self.broker.get_cash()
        price = Decimal(str(quote.last_price))
        qty = (cash / price).quantize(Decimal("0.01"))
        if qty <= 0:
            return

        result = self.broker.submit_limit_order(symbol, "SELL", qty, price)
        self._record_order(result.broker_order_id, symbol, "SELL", float(qty), float(price))
        self.notifier.notify_order("SELL_SHORT", symbol, str(qty), str(price), result.broker_order_id)
        logger.info(f"SELL_SHORT: {symbol} qty={qty} price={price}")

    def _execute_buy_to_cover(self, symbol: str, quote: Quote) -> None:
        positions = self.broker.get_positions()
        long_pos = next((p for p in positions if p.symbol == symbol and p.side == "SHORT" and p.quantity > 0), None)
        if long_pos is None:
            long_pos = next((p for p in positions if p.symbol == symbol and p.side == "LONG" and p.quantity > 0), None)
        if long_pos is None:
            return

        price = Decimal(str(quote.last_price))
        result = self.broker.submit_limit_order(symbol, "BUY", long_pos.quantity, price)
        self._record_order(result.broker_order_id, symbol, "BUY", float(long_pos.quantity), float(price))
        self.notifier.notify_order("BUY_TO_COVER", symbol, str(long_pos.quantity), str(price), result.broker_order_id)
        logger.info(f"BUY_TO_COVER: {symbol} qty={long_pos.quantity} price={price}")

    def _record_order(self, order_id: str, symbol: str, side: str, qty: float, price: float) -> None:
        db = SessionLocal()
        try:
            order = OrderRecord(
                broker_order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=qty,
                price=price,
                status="SUBMITTED",
            )
            db.add(order)
            db.commit()
        finally:
            db.close()

    def _record_risk_event(self, reason: str) -> None:
        db = SessionLocal()
        try:
            event = RiskEvent(event_type="RISK_REJECTION", reason=reason)
            db.add(event)
            db.commit()
        finally:
            db.close()

    def _broadcast_status(self) -> None:
        try:
            data = self.engine.to_dict()
            data["risks"] = {
                "daily_pnl": self.risk.daily_pnl,
                "consecutive_losses": self.risk.consecutive_losses,
                "kill_switch": self.risk.kill_switch,
                "paused": self.risk.paused,
            }
            asyncio.run(manager.broadcast(data))
        except Exception:
            pass

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._persist_state()
            except Exception:
                logger.exception("error persisting state")
            import time
            time.sleep(5)

    def _persist_state(self) -> None:
        db = SessionLocal()
        try:
            svc = StrategyService(db)
            svc.update_runtime_state(
                engine_state=self.engine.state,
                last_price=self.engine.last_price,
                daily_pnl=self.risk.daily_pnl,
                consecutive_losses=self.risk.consecutive_losses,
                kill_switch=self.risk.kill_switch,
                paused=self.risk.paused,
            )
        finally:
            db.close()


_runner: AppRunner | None = None


def get_runner() -> AppRunner:
    global _runner
    if _runner is None:
        _runner = AppRunner()
    return _runner
```

- [ ] **Step 2: Write `backend/docker-entrypoint.sh`**

```bash
#!/bin/bash
set -e

cd /app
python -c "from app.database import init_db; init_db()"

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- [ ] **Step 3: Make entrypoint executable**

```bash
chmod +x backend/docker-entrypoint.sh
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/runner.py backend/docker-entrypoint.sh
git commit -m "feat: add application runner with quote loop and order execution"
```

---

### Task 11: Frontend Project Skeleton

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.node.json`
- Create: `frontend/index.html`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`

- [ ] **Step 1: Write `frontend/package.json`**

```json
{
  "name": "auto-trade-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "vue": "^3.5.0",
    "vue-router": "^4.4.0",
    "element-plus": "^2.8.0",
    "axios": "^1.7.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.1.0",
    "typescript": "^5.5.0",
    "vite": "^5.4.0",
    "vue-tsc": "^2.1.0"
  }
}
```

- [ ] **Step 2: Write `frontend/vite.config.ts`**

```typescript
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
})
```

- [ ] **Step 3: Write `frontend/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "jsx": "preserve",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "esModuleInterop": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "skipLibCheck": true,
    "noEmit": true,
    "paths": {
      "@/*": ["./src/*"]
    }
  },
  "include": ["src/**/*.ts", "src/**/*.d.ts", "src/**/*.tsx", "src/**/*.vue"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

- [ ] **Step 4: Write `frontend/tsconfig.node.json`**

```json
{
  "compilerOptions": {
    "composite": true,
    "skipLibCheck": true,
    "module": "ESNext",
    "moduleResolution": "bundler",
    "allowSyntheticDefaultImports": true
  },
  "include": ["vite.config.ts"]
}
```

- [ ] **Step 5: Write `frontend/index.html`**

```html
<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Auto Trade</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
```

- [ ] **Step 6: Write `frontend/src/main.ts`**

```typescript
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import router from './router'

const app = createApp(App)
app.use(ElementPlus)
app.use(router)
app.mount('#app')
```

- [ ] **Step 7: Write `frontend/src/App.vue`**

```vue
<template>
  <el-container>
    <el-header>
      <h2>Auto Trade</h2>
      <el-menu mode="horizontal" :default-active="route.path" router>
        <el-menu-item index="/">Dashboard</el-menu-item>
        <el-menu-item index="/strategy">Strategy</el-menu-item>
        <el-menu-item index="/history">Trade History</el-menu-item>
      </el-menu>
    </el-header>
    <el-main>
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
const route = useRoute()
</script>

<style>
body { margin: 0; font-family: sans-serif; }
.el-header { display: flex; align-items: center; gap: 24px; }
.el-header h2 { margin: 0; }
</style>
```

- [ ] **Step 8: Commit**

```bash
git add frontend/
git commit -m "feat: add frontend project skeleton with Vue 3 + Element Plus"
```

---

### Task 12: Frontend Router and API Layer

**Files:**
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/api/index.ts`
- Create: `frontend/src/types/index.ts`

- [ ] **Step 1: Write `frontend/src/router/index.ts`**

```typescript
import { createRouter, createWebHashHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import Strategy from '../views/Strategy.vue'
import TradeHistory from '../views/TradeHistory.vue'

const routes = [
  { path: '/', component: Dashboard },
  { path: '/strategy', component: Strategy },
  { path: '/history', component: TradeHistory },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
```

- [ ] **Step 2: Write `frontend/src/types/index.ts`**

```typescript
export interface StrategyConfig {
  id: number
  symbol: string
  market: 'US' | 'HK'
  buy_low: number
  sell_high: number
  short_selling: boolean
  max_daily_loss: number
  max_consecutive_losses: number
  sct_key: string
  updated_at: string
}

export interface StatusData {
  engine_state: string
  paused: boolean
  kill_switch: boolean
  daily_pnl: number
  consecutive_losses: number
  last_price: number
  last_trigger_price: number
  last_trigger_at: string | null
}

export interface OrderRecord {
  id: number
  broker_order_id: string
  symbol: string
  side: string
  quantity: number
  price: number
  status: string
  created_at: string
  filled_at: string | null
}
```

- [ ] **Step 3: Write `frontend/src/api/index.ts`**

```typescript
import axios from 'axios'
import type { StrategyConfig, StatusData, OrderRecord } from '../types'

const api = axios.create({ baseURL: '' })

export async function getStrategy(): Promise<StrategyConfig> {
  const resp = await api.get('/api/strategy')
  return resp.data
}

export async function updateStrategy(data: Partial<StrategyConfig>): Promise<StrategyConfig> {
  const resp = await api.put('/api/strategy', data)
  return resp.data
}

export async function getStatus(): Promise<StatusData> {
  const resp = await api.get('/api/status')
  return resp.data
}

export async function getOrders(limit = 50): Promise<OrderRecord[]> {
  const resp = await api.get('/api/orders', { params: { limit } })
  return resp.data
}

export async function pauseTrading(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/pause', { reason })
  return resp.data
}

export async function resumeTrading(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/resume')
  return resp.data
}

export async function activateKillSwitch(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/kill-switch', { reason })
  return resp.data
}

export async function startTrading(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/start')
  return resp.data
}

export async function stopTrading(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/stop', { reason })
  return resp.data
}
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/router/ frontend/src/api/ frontend/src/types/
git commit -m "feat: add frontend router, API layer, and types"
```

---

### Task 13: Frontend Dashboard View

**Files:**
- Create: `frontend/src/views/Dashboard.vue`

- [ ] **Step 1: Write `frontend/src/views/Dashboard.vue`**

```vue
<template>
  <div>
    <h3>Dashboard</h3>
    <el-row :gutter="20">
      <el-col :span="8">
        <el-card>
          <template #header>Engine State</template>
          <el-tag :type="stateTagType">{{ status.engine_state.toUpperCase() }}</el-tag>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header>Last Price</template>
          <h1>${{ status.last_price.toFixed(2) }}</h1>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header>Daily P&L</template>
          <h1 :style="{ color: status.daily_pnl >= 0 ? 'green' : 'red' }">
            ${{ status.daily_pnl.toFixed(2) }}
          </h1>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" style="margin-top: 20px">
      <el-col :span="12">
        <el-card>
          <template #header>Risk Status</template>
          <p>Kill Switch: <el-tag :type="status.kill_switch ? 'danger' : 'success'">{{ status.kill_switch ? 'ON' : 'OFF' }}</el-tag></p>
          <p>Paused: <el-tag :type="status.paused ? 'warning' : 'success'">{{ status.paused ? 'YES' : 'NO' }}</el-tag></p>
          <p>Consecutive Losses: {{ status.consecutive_losses }}</p>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>Controls</template>
          <el-space>
            <el-button type="primary" @click="handleStart">Start</el-button>
            <el-button type="danger" @click="handleStop">Stop</el-button>
            <el-button type="warning" @click="handlePause" :disabled="status.paused">Pause</el-button>
            <el-button type="success" @click="handleResume" :disabled="!status.paused">Resume</el-button>
            <el-button type="danger" plain @click="handleKillSwitch">Kill Switch</el-button>
          </el-space>
        </el-card>
      </el-col>
    </el-row>

    <el-card style="margin-top: 20px">
      <template #header>Quote Info</template>
      <p>Symbol: {{ strategy.symbol || 'Not configured' }}</p>
      <p>Market: {{ strategy.market }}</p>
      <p>Buy Low: ${{ strategy.buy_low }}</p>
      <p>Sell High: ${{ strategy.sell_high }}</p>
      <p>Short Selling: {{ strategy.short_selling ? 'Yes' : 'No' }}</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { getStrategy, getStatus, pauseTrading, resumeTrading, activateKillSwitch, startTrading, stopTrading } from '../api'
import type { StrategyConfig, StatusData } from '../types'

const strategy = ref<StrategyConfig>({
  id: 0, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
  short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
  sct_key: '', updated_at: '',
})

const status = ref<StatusData>({
  engine_state: 'flat', paused: false, kill_switch: false,
  daily_pnl: 0, consecutive_losses: 0,
  last_price: 0, last_trigger_price: 0, last_trigger_at: null,
})

const stateTagType = computed(() => {
  switch (status.value.engine_state) {
    case 'long': return 'success'
    case 'short': return 'danger'
    default: return 'info'
  }
})

let timer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  await refresh()
  timer = setInterval(refresh, 3000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

async function refresh() {
  try {
    const [s, st] = await Promise.all([getStrategy(), getStatus()])
    strategy.value = s
    status.value = st
  } catch (e) {
    console.error('Failed to refresh:', e)
  }
}

async function handlePause() {
  await pauseTrading()
  await refresh()
}

async function handleResume() {
  await resumeTrading()
  await refresh()
}

async function handleKillSwitch() {
  await activateKillSwitch()
  await refresh()
}

async function handleStart() {
  await startTrading()
  await refresh()
}

async function handleStop() {
  await stopTrading()
  await refresh()
}
</script>
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/views/Dashboard.vue
git commit -m "feat: add dashboard view with status and controls"
```

---

### Task 14: Frontend Strategy Config View

**Files:**
- Create: `frontend/src/views/Strategy.vue`

- [ ] **Step 1: Write `frontend/src/views/Strategy.vue`**

```vue
<template>
  <div>
    <h3>Strategy Configuration</h3>
    <el-card style="max-width: 600px">
      <el-form :model="form" label-width="180px" @submit.prevent="handleSave">
        <el-form-item label="Symbol">
          <el-input v-model="form.symbol" placeholder="e.g. AAPL.US" />
        </el-form-item>
        <el-form-item label="Market">
          <el-radio-group v-model="form.market">
            <el-radio value="US">US</el-radio>
            <el-radio value="HK">HK</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="Buy Low Price">
          <el-input-number v-model="form.buy_low" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="Sell High Price">
          <el-input-number v-model="form.sell_high" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="Short Selling">
          <el-switch v-model="form.short_selling" />
        </el-form-item>
        <el-form-item label="Max Daily Loss">
          <el-input-number v-model="form.max_daily_loss" :min="1" :precision="2" />
        </el-form-item>
        <el-form-item label="Max Consecutive Losses">
          <el-input-number v-model="form.max_consecutive_losses" :min="1" />
        </el-form-item>
        <el-form-item label="ServerChan SCT Key">
          <el-input v-model="form.sct_key" placeholder="SCT key for notifications" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSave" :loading="saving">Save</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">Saved!</el-tag>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getStrategy, updateStrategy } from '../api'

const form = ref({
  symbol: '',
  market: 'US' as 'US' | 'HK',
  buy_low: 0,
  sell_high: 0,
  short_selling: false,
  max_daily_loss: 5000,
  max_consecutive_losses: 3,
  sct_key: '',
})

const saving = ref(false)
const saved = ref(false)

onMounted(async () => {
  const s = await getStrategy()
  form.value = {
    symbol: s.symbol,
    market: s.market,
    buy_low: s.buy_low,
    sell_high: s.sell_high,
    short_selling: s.short_selling,
    max_daily_loss: s.max_daily_loss,
    max_consecutive_losses: s.max_consecutive_losses,
    sct_key: s.sct_key,
  }
})

async function handleSave() {
  saving.value = true
  saved.value = false
  try {
    await updateStrategy(form.value)
    saved.value = true
  } catch (e) {
    console.error('Save failed:', e)
  } finally {
    saving.value = false
  }
}
</script>
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/views/Strategy.vue
git commit -m "feat: add strategy configuration view with form"
```

---

### Task 15: Frontend Trade History View

**Files:**
- Create: `frontend/src/views/TradeHistory.vue`

- [ ] **Step 1: Write `frontend/src/views/TradeHistory.vue`**

```vue
<template>
  <div>
    <h3>Trade History</h3>
    <el-table :data="orders" stripe style="width: 100%">
      <el-table-column prop="broker_order_id" label="Order ID" width="180" />
      <el-table-column prop="symbol" label="Symbol" width="120" />
      <el-table-column prop="side" label="Side" width="100">
        <template #default="{ row }">
          <el-tag :type="row.side === 'BUY' ? 'success' : 'danger'">{{ row.side }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="quantity" label="Quantity" width="120" />
      <el-table-column prop="price" label="Price" width="100" />
      <el-table-column prop="status" label="Status" width="120">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="Created At" width="200" />
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getOrders } from '../api'
import type { OrderRecord } from '../types'

const orders = ref<OrderRecord[]>([])

onMounted(async () => {
  orders.value = await getOrders(100)
})

function statusType(status: string): string {
  switch (status) {
    case 'FILLED': return 'success'
    case 'SUBMITTED': return 'warning'
    case 'REJECTED': case 'CANCELLED': return 'info'
    default: return ''
  }
}
</script>
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/views/TradeHistory.vue
git commit -m "feat: add trade history view"
```

---

### Task 16: Docker Configuration

**Files:**
- Create: `backend/Dockerfile`
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`
- Create: `docker-compose.yaml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `README.md`

- [ ] **Step 1: Write `backend/Dockerfile`**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY docker-entrypoint.sh .

RUN mkdir -p data

EXPOSE 8000

CMD ["./docker-entrypoint.sh"]
```

- [ ] **Step 2: Write `frontend/Dockerfile`**

```dockerfile
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
```

- [ ] **Step 3: Write `frontend/nginx.conf`**

```nginx
server {
    listen 80;
    server_name localhost;

    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /ws {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

- [ ] **Step 4: Write `docker-compose.yaml`**

```yaml
version: '3.8'

services:
  backend:
    build: ./backend
    ports:
      - '8000:8000'
    volumes:
      - ./data:/app/data
    environment:
      - AUTO_TRADE_ENV=${AUTO_TRADE_ENV:-dev}
      - AUTO_TRADE_DATABASE_URL=sqlite:///app/data/auto_trade.db
      - LONGBRIDGE_APP_KEY=${LONGBRIDGE_APP_KEY:-}
      - LONGBRIDGE_APP_SECRET=${LONGBRIDGE_APP_SECRET:-}
      - LONGBRIDGE_ACCESS_TOKEN=${LONGBRIDGE_ACCESS_TOKEN:-}
      - AUTO_TRADE_SCT_KEY=${AUTO_TRADE_SCT_KEY:-}
    restart: unless-stopped

  frontend:
    build: ./frontend
    ports:
      - '8080:80'
    depends_on:
      - backend
    restart: unless-stopped
```

- [ ] **Step 5: Write `.env.example`**

```
AUTO_TRADE_ENV=dev
LONGBRIDGE_APP_KEY=
LONGBRIDGE_APP_SECRET=
LONGBRIDGE_ACCESS_TOKEN=
AUTO_TRADE_SCT_KEY=
```

- [ ] **Step 6: Write `.gitignore`**

```
# Python
__pycache__/
*.pyc
.venv/
*.egg-info/
dist/

# SQLite
*.db

# Data
data/

# Frontend
node_modules/
frontend/dist/

# Environment
.env
!.env.example

# IDE
.idea/
.vscode/
*.swp

# Logs
*.log

# Docker
docker-compose.override.yaml

# OS
.DS_Store
Thumbs.db
```

- [ ] **Step 7: Write `README.md`**

```markdown
# Auto Trade

基于长桥 (Longbridge) SDK 的自动化区间交易系统。

## 功能

- 单标的区间交易：设定最低价买入、最高价卖出
- 可选做空支持
- 实时行情驱动交易
- 简单风控（单日最大亏损、连续亏损限制）
- Web UI 配置和监控
- Server酱通知
- Docker Compose 一键部署

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入长桥凭证和 Server酱 Key
```

### 2. 启动服务

```bash
docker compose up --build -d
```

- 前端: http://localhost:8080
- 后端 API: http://localhost:8000

### 3. 配置策略

打开 Web UI，进入 Strategy 页面配置:
- 股票代码 (e.g. AAPL.US)
- 最低买入价
- 最高卖出价
- 是否开启做空
- 风控参数

### 本地开发

后端:

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

前端:

```bash
cd frontend
npm install
npm run dev
```

## 测试

```bash
cd backend && python -m pytest tests/ -v
```
```

- [ ] **Step 8: Commit**

```bash
git add backend/Dockerfile frontend/Dockerfile frontend/nginx.conf docker-compose.yaml .env.example .gitignore README.md
git commit -m "feat: add Docker configuration and documentation"
```
