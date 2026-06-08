# P41 Trade Reports Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Phase 1 trusted trade reports foundation: connected backend/frontend routes, typed report schema, correct core metrics, daily/cumulative PnL curve data, max drawdown, export, and tests.

**Architecture:** Build on the existing uncommitted Reports draft. `ReportService` remains the single backend aggregation unit and delegates realized PnL calculation to `DailyPnlService`; FastAPI exposes typed report responses through `app.schemas`. The frontend uses a dedicated `api/reports.ts` client, typed `ReportResponse`, and a single `Reports.vue` page with pure SVG charts.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 sync ORM, Pydantic v2, pytest, Vue 3.5, TypeScript strict, Element Plus, Cypress.

**Scope:** This plan implements Phase 1 only. It pre-wires stable `attribution: []` and `details: []` fields for Phase 2/3, but does not implement attribution calculations or drill-down details.

**Git rule:** Do not commit unless the user explicitly asks. Where this plan says “checkpoint”, inspect `git diff` instead of creating a commit.

---

## File Structure

- Modify `backend/app/schemas.py`
  - Add Pydantic report response models near other response schemas.
- Modify `backend/app/services/report_service.py`
  - Replace draft metric calculations with Phase 1 metrics backed by `DailyPnlService`.
  - Add cumulative PnL and drawdown per day.
  - Return stable empty attribution/details arrays.
- Modify `backend/app/api/reports.py`
  - Keep existing endpoints, ensure robust 400 handling and typed responses.
- Modify `backend/app/main.py`
  - Import and include `reports_router`.
- Create `backend/tests/test_report_service.py`
  - Service-level TDD tests for metrics and inclusive range behavior.
- Create `backend/tests/test_reports_api.py`
  - API and export tests.
- Modify `frontend/src/types/index.ts`
  - Add report response interfaces.
- Modify `frontend/src/api/reports.ts`
  - Keep typed report API helpers.
- Modify `frontend/src/router/index.ts`
  - Add `/reports` route.
- Modify `frontend/src/App.vue`
  - Add desktop Reports navigation link.
- Modify `frontend/src/views/Reports.vue`
  - Add default date range, validation, cumulative line chart, max drawdown card, stable typed rendering.
- Modify `frontend/cypress/support/e2e.ts`
  - Add default reports intercepts so global app stubs remain complete.
- Modify `frontend/cypress/e2e/reports.cy.ts`
  - Extend Reports page E2E coverage.

---

### Task 1: Backend schemas and router registration

**Files:**
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/main.py`
- Test later: `backend/tests/test_reports_api.py`

- [ ] **Step 1: Add report schemas**

Add these classes in `backend/app/schemas.py` after `StatusHistoryResponse` and before diagnostic schemas:

```python
class ReportMetrics(BaseModel):
    total_pnl: float
    total_trades: int
    win_count: int
    loss_count: int
    win_rate: float
    profit_loss_ratio: float
    avg_pnl_per_trade: float
    max_profit: float
    max_loss: float
    max_drawdown: float
    llm_suggestions_count: int
    llm_applied_count: int
    llm_apply_rate: float
    llm_profitable_count: int
    llm_accuracy_rate: float


class ReportDailyPoint(BaseModel):
    date: str
    pnl: float
    cumulative_pnl: float
    drawdown: float
    trade_count: int
    win_count: int


class ReportAttributionPoint(BaseModel):
    key: str
    label: str
    trade_count: int
    pnl: float
    win_rate: float
    share: float


class ReportOrderDetail(BaseModel):
    broker_order_id: str
    side: str
    quantity: float
    executed_price: float
    status: str
    filled_at: datetime | None
    pnl: float


class ReportDayDetail(BaseModel):
    date: str
    orders: list[ReportOrderDetail]


class ReportResponse(BaseModel):
    period_type: str
    symbol: str
    start_date: str
    end_date: str
    metrics: ReportMetrics
    daily_points: list[ReportDailyPoint]
    attribution: list[ReportAttributionPoint] = Field(default_factory=list)
    details: list[ReportDayDetail] = Field(default_factory=list)
```

- [ ] **Step 2: Register the reports router**

In `backend/app/main.py`, add this import with the other API router imports:

```python
from app.api.reports import router as reports_router
```

Then add this include before `review_router` or before `ws_router`:

```python
app.include_router(reports_router)
```

- [ ] **Step 3: Run schema import smoke test**

Run from `backend/`:

```bash
python3 - <<'PY'
from app.schemas import ReportResponse
from app.main import app
paths = {route.path for route in app.routes}
assert '/api/reports/range' in paths
print(ReportResponse.__name__)
PY
```

Expected output includes:

```text
ReportResponse
```

- [ ] **Step 4: Checkpoint**

Run from repo root:

```bash
git diff -- backend/app/schemas.py backend/app/main.py
```

Expected: diff only contains report schemas and router registration.

---

### Task 2: ReportService Phase 1 metrics

**Files:**
- Modify: `backend/app/services/report_service.py`
- Create: `backend/tests/test_report_service.py`

- [ ] **Step 1: Write service tests**

Create `backend/tests/test_report_service.py` with this content:

```python
from __future__ import annotations

import os
from datetime import date, datetime, time, timezone

from pytest import approx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_report_service.db"

from app.models import Base, LLMInteraction, OrderRecord
from app.services.report_service import ReportService


class TestReportService:
    @classmethod
    def setup_class(cls) -> None:
        engine = create_engine(os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False})
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.engine = engine

    def _get_db(self) -> Session:
        return Session(bind=self.engine)

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(LLMInteraction).delete()
        db.query(OrderRecord).delete()
        db.commit()
        db.close()

    def _dt(self, day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)

    def _filled_order(self, broker_order_id: str, symbol: str, side: str, qty: float, price: float, day: date, hour: int) -> OrderRecord:
        return OrderRecord(
            broker_order_id=broker_order_id,
            symbol=symbol,
            side=side,
            quantity=qty,
            price=price,
            executed_quantity=qty,
            executed_price=price,
            status="FILLED",
            created_at=self._dt(day, hour),
            filled_at=self._dt(day, hour, 1),
        )

    def test_empty_range_returns_zero_metrics_and_empty_arrays(self) -> None:
        self._cleanup()
        db = self._get_db()

        report = ReportService(db).get_range_report("AAPL.US", "2026-06-01", "2026-06-03")
        db.close()

        assert report.metrics.total_pnl == 0.0
        assert report.metrics.total_trades == 0
        assert report.metrics.max_drawdown == 0.0
        assert report.daily_points == []
        assert report.attribution == []
        assert report.details == []

    def test_computes_core_metrics_cumulative_pnl_and_drawdown(self) -> None:
        self._cleanup()
        day1 = date(2026, 6, 1)
        day2 = date(2026, 6, 2)
        day3 = date(2026, 6, 3)
        db = self._get_db()
        db.add_all([
            self._filled_order("buy-1", "AAPL.US", "BUY", 10, 100, day1, 10),
            self._filled_order("sell-1", "AAPL.US", "SELL", 10, 110, day1, 11),
            self._filled_order("buy-2", "AAPL.US", "BUY", 5, 100, day2, 10),
            self._filled_order("sell-2", "AAPL.US", "SELL", 5, 90, day2, 11),
            self._filled_order("buy-3", "AAPL.US", "BUY", 2, 100, day3, 10),
            self._filled_order("sell-3", "AAPL.US", "SELL", 2, 103, day3, 11),
            self._filled_order("buy-other", "MSFT.US", "BUY", 1, 100, day1, 12),
            self._filled_order("sell-other", "MSFT.US", "SELL", 1, 200, day1, 13),
        ])
        db.commit()

        report = ReportService(db).get_range_report("AAPL.US", "2026-06-01", "2026-06-03")
        db.close()

        assert report.metrics.total_pnl == approx(56.0)
        assert report.metrics.total_trades == 3
        assert report.metrics.win_count == 2
        assert report.metrics.loss_count == 1
        assert report.metrics.win_rate == approx(0.6667)
        assert report.metrics.avg_pnl_per_trade == approx(18.67)
        assert report.metrics.max_profit == approx(100.0)
        assert report.metrics.max_loss == approx(-50.0)
        assert report.metrics.profit_loss_ratio == approx(1.06)
        assert report.metrics.max_drawdown == approx(50.0)
        assert [(p.date, p.pnl, p.cumulative_pnl, p.drawdown) for p in report.daily_points] == [
            ("2026-06-01", approx(100.0), approx(100.0), approx(0.0)),
            ("2026-06-02", approx(-50.0), approx(50.0), approx(50.0)),
            ("2026-06-03", approx(6.0), approx(56.0), approx(44.0)),
        ]

    def test_counts_llm_suggestions_and_applied_rate(self) -> None:
        self._cleanup()
        db = self._get_db()
        db.add_all([
            LLMInteraction(symbol="AAPL.US", applied=True, created_at=self._dt(date(2026, 6, 1), 10)),
            LLMInteraction(symbol="AAPL.US", applied=False, created_at=self._dt(date(2026, 6, 1), 11)),
            LLMInteraction(symbol="MSFT.US", applied=True, created_at=self._dt(date(2026, 6, 1), 12)),
        ])
        db.commit()

        report = ReportService(db).get_range_report("AAPL.US", "2026-06-01", "2026-06-01")
        db.close()

        assert report.metrics.llm_suggestions_count == 2
        assert report.metrics.llm_applied_count == 1
        assert report.metrics.llm_apply_rate == approx(0.5)
        assert report.metrics.llm_profitable_count == 0
        assert report.metrics.llm_accuracy_rate == 0.0
```

- [ ] **Step 2: Run tests to verify current draft fails**

Run from `backend/`:

```bash
python3 -m pytest tests/test_report_service.py -v
```

Expected before implementation: failures mentioning missing `attribution`/`details` or incorrect metrics such as `max_drawdown`/`max_profit`.

- [ ] **Step 3: Update report dataclasses**

In `backend/app/services/report_service.py`, replace the existing report dataclasses with this complete set:

```python
@dataclass(frozen=True)
class DailyPnLPoint:
    date: str
    pnl: float
    cumulative_pnl: float
    drawdown: float
    trade_count: int
    win_count: int


@dataclass(frozen=True)
class ReportMetrics:
    total_pnl: float
    total_trades: int
    win_count: int
    loss_count: int
    win_rate: float
    profit_loss_ratio: float
    avg_pnl_per_trade: float
    max_profit: float
    max_loss: float
    max_drawdown: float
    llm_suggestions_count: int
    llm_applied_count: int
    llm_apply_rate: float
    llm_profitable_count: int
    llm_accuracy_rate: float


@dataclass(frozen=True)
class ReportAttributionPoint:
    key: str
    label: str
    trade_count: int
    pnl: float
    win_rate: float
    share: float


@dataclass(frozen=True)
class ReportDayDetail:
    date: str
    orders: list[dict[str, Any]]


@dataclass(frozen=True)
class PeriodReport:
    period_type: str
    symbol: str
    start_date: str
    end_date: str
    metrics: ReportMetrics
    daily_points: list[DailyPnLPoint]
    attribution: list[ReportAttributionPoint]
    details: list[ReportDayDetail]
```

- [ ] **Step 4: Replace `_build_report` body**

Replace `ReportService._build_report` with:

```python
    def _build_report(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        period_type: str,
        start_date_str: str,
        end_date_str: str,
    ) -> PeriodReport:
        llms = self._load_llm_interactions(symbol, start, end)
        pnl_service = DailyPnlService(self._db)
        start_day = start.date()
        end_day = (end - timedelta(days=1)).date()

        daily_points: list[DailyPnLPoint] = []
        all_trade_pnls: list[float] = []
        cumulative_pnl = 0.0
        peak_cumulative_pnl = 0.0
        max_drawdown = 0.0
        total_wins = 0

        current_day = start_day
        while current_day <= end_day:
            result = pnl_service.calculate(trade_day=current_day, symbol=symbol)
            day_pnl = result.realized_pnl
            day_trades = len(result.trades)
            day_wins = sum(1 for trade in result.trades if trade.pnl > 0)
            if day_trades > 0 or abs(day_pnl) > 0:
                cumulative_pnl += day_pnl
                peak_cumulative_pnl = max(peak_cumulative_pnl, cumulative_pnl)
                drawdown = peak_cumulative_pnl - cumulative_pnl
                max_drawdown = max(max_drawdown, drawdown)
                daily_points.append(
                    DailyPnLPoint(
                        date=current_day.isoformat(),
                        pnl=round(day_pnl, 2),
                        cumulative_pnl=round(cumulative_pnl, 2),
                        drawdown=round(drawdown, 2),
                        trade_count=day_trades,
                        win_count=day_wins,
                    )
                )
                all_trade_pnls.extend(trade.pnl for trade in result.trades)
                total_wins += day_wins
            current_day += timedelta(days=1)

        metrics = self._compute_metrics_from_trades(
            trade_pnls=all_trade_pnls,
            win_count=total_wins,
            llms=llms,
            max_drawdown=max_drawdown,
        )

        return PeriodReport(
            period_type=period_type,
            symbol=symbol,
            start_date=start_date_str,
            end_date=end_date_str,
            metrics=metrics,
            daily_points=daily_points,
            attribution=[],
            details=[],
        )
```

- [ ] **Step 5: Replace metrics helper**

Replace `_compute_metrics_from_totals` with:

```python
    def _compute_metrics_from_trades(
        self,
        trade_pnls: list[float],
        win_count: int,
        llms: list[_LLMRec],
        max_drawdown: float,
    ) -> ReportMetrics:
        total_trades = len(trade_pnls)
        total_pnl = sum(trade_pnls)
        loss_count = total_trades - win_count
        win_rate = win_count / total_trades if total_trades > 0 else 0.0
        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0.0
        profits = [pnl for pnl in trade_pnls if pnl > 0]
        losses = [pnl for pnl in trade_pnls if pnl < 0]
        avg_profit = sum(profits) / len(profits) if profits else 0.0
        avg_loss_abs = abs(sum(losses) / len(losses)) if losses else 0.0
        profit_loss_ratio = avg_profit / avg_loss_abs if avg_loss_abs > 0 else 0.0

        llm_total = len(llms)
        llm_applied = sum(1 for item in llms if item.applied)
        llm_apply_rate = llm_applied / llm_total if llm_total > 0 else 0.0

        return ReportMetrics(
            total_pnl=round(total_pnl, 2),
            total_trades=total_trades,
            win_count=win_count,
            loss_count=loss_count,
            win_rate=round(win_rate, 4),
            profit_loss_ratio=round(profit_loss_ratio, 2),
            avg_pnl_per_trade=round(avg_pnl, 2),
            max_profit=round(max(profits), 2) if profits else 0.0,
            max_loss=round(min(losses), 2) if losses else 0.0,
            max_drawdown=round(max_drawdown, 2),
            llm_suggestions_count=llm_total,
            llm_applied_count=llm_applied,
            llm_apply_rate=round(llm_apply_rate, 4),
            llm_profitable_count=0,
            llm_accuracy_rate=0.0,
        )
```

- [ ] **Step 6: Update `_report_to_dict`**

In `_report_to_dict`, include `max_drawdown`, `cumulative_pnl`, `drawdown`, `attribution`, and `details`:

```python
    @staticmethod
    def _report_to_dict(report: PeriodReport) -> dict[str, Any]:
        return {
            "period_type": report.period_type,
            "symbol": report.symbol,
            "start_date": report.start_date,
            "end_date": report.end_date,
            "metrics": {
                "total_pnl": report.metrics.total_pnl,
                "total_trades": report.metrics.total_trades,
                "win_count": report.metrics.win_count,
                "loss_count": report.metrics.loss_count,
                "win_rate": report.metrics.win_rate,
                "profit_loss_ratio": report.metrics.profit_loss_ratio,
                "avg_pnl_per_trade": report.metrics.avg_pnl_per_trade,
                "max_profit": report.metrics.max_profit,
                "max_loss": report.metrics.max_loss,
                "max_drawdown": report.metrics.max_drawdown,
                "llm_suggestions_count": report.metrics.llm_suggestions_count,
                "llm_applied_count": report.metrics.llm_applied_count,
                "llm_apply_rate": report.metrics.llm_apply_rate,
                "llm_profitable_count": report.metrics.llm_profitable_count,
                "llm_accuracy_rate": report.metrics.llm_accuracy_rate,
            },
            "daily_points": [
                {
                    "date": point.date,
                    "pnl": point.pnl,
                    "cumulative_pnl": point.cumulative_pnl,
                    "drawdown": point.drawdown,
                    "trade_count": point.trade_count,
                    "win_count": point.win_count,
                }
                for point in report.daily_points
            ],
            "attribution": [
                {
                    "key": item.key,
                    "label": item.label,
                    "trade_count": item.trade_count,
                    "pnl": item.pnl,
                    "win_rate": item.win_rate,
                    "share": item.share,
                }
                for item in report.attribution
            ],
            "details": [
                {"date": item.date, "orders": item.orders}
                for item in report.details
            ],
        }
```

- [ ] **Step 7: Update CSV export headers**

In `export_report`, for CSV write these headers and rows:

```python
        writer.writerow([
            "date", "symbol", "trade_count", "win_count", "pnl", "cumulative_pnl", "drawdown",
        ])
        for point in report.daily_points:
            writer.writerow([
                point.date,
                symbol,
                point.trade_count,
                point.win_count,
                point.pnl,
                point.cumulative_pnl,
                point.drawdown,
            ])
```

- [ ] **Step 8: Run service tests**

Run from `backend/`:

```bash
python3 -m pytest tests/test_report_service.py -v
```

Expected: all tests pass.

- [ ] **Step 9: Checkpoint**

Run from repo root:

```bash
git diff -- backend/app/services/report_service.py backend/tests/test_report_service.py
```

Expected: diff implements only Phase 1 report metrics and tests.

---

### Task 3: Reports API tests and endpoint hardening

**Files:**
- Modify: `backend/app/api/reports.py`
- Create: `backend/tests/test_reports_api.py`

- [ ] **Step 1: Write API tests**

Create `backend/tests/test_reports_api.py`:

```python
from __future__ import annotations

import os
from datetime import date, datetime, time, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_reports_api.db"

from app.database import SessionLocal, engine
from app.main import app
from app.models import Base, OrderRecord


class TestReportsApi:
    @classmethod
    def setup_class(cls) -> None:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

    def _cleanup(self) -> None:
        db = SessionLocal()
        db.query(OrderRecord).delete()
        db.commit()
        db.close()

    def _dt(self, day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)

    def _seed_round_trip(self) -> None:
        self._cleanup()
        db: Session = SessionLocal()
        day = date(2026, 6, 1)
        db.add_all([
            OrderRecord(
                broker_order_id="api-buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=2,
                price=100,
                executed_quantity=2,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(day, 10),
                filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="api-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=2,
                price=105,
                executed_quantity=2,
                executed_price=105,
                status="FILLED",
                created_at=self._dt(day, 11),
                filled_at=self._dt(day, 11, 1),
            ),
        ])
        db.commit()
        db.close()

    def test_range_report_returns_schema(self) -> None:
        self._seed_round_trip()

        response = self.client.get("/api/reports/range?symbol=AAPL.US&from_date=2026-06-01&to_date=2026-06-01")

        assert response.status_code == 200
        data = response.json()
        assert data["period_type"] == "range"
        assert data["symbol"] == "AAPL.US"
        assert data["metrics"]["total_pnl"] == 10.0
        assert data["metrics"]["max_drawdown"] == 0.0
        assert data["daily_points"][0]["cumulative_pnl"] == 10.0
        assert data["attribution"] == []
        assert data["details"] == []

    def test_daily_weekly_monthly_endpoints_return_200(self) -> None:
        self._seed_round_trip()

        assert self.client.get("/api/reports/daily?symbol=AAPL.US&date=2026-06-01").status_code == 200
        assert self.client.get("/api/reports/weekly?symbol=AAPL.US&week_start=2026-06-01").status_code == 200
        assert self.client.get("/api/reports/monthly?symbol=AAPL.US&month=2026-06").status_code == 200

    def test_export_json_and_csv(self) -> None:
        self._seed_round_trip()

        json_response = self.client.get("/api/reports/export?symbol=AAPL.US&from_date=2026-06-01&to_date=2026-06-01&format=json")
        csv_response = self.client.get("/api/reports/export?symbol=AAPL.US&from_date=2026-06-01&to_date=2026-06-01&format=csv")

        assert json_response.status_code == 200
        assert json_response.headers["content-type"].startswith("application/json")
        assert csv_response.status_code == 200
        assert csv_response.headers["content-type"].startswith("text/csv")
        assert "cumulative_pnl" in csv_response.text

    def test_invalid_format_and_invalid_date_return_400(self) -> None:
        bad_format = self.client.get("/api/reports/export?symbol=AAPL.US&from_date=2026-06-01&to_date=2026-06-01&format=xlsx")
        bad_date = self.client.get("/api/reports/range?symbol=AAPL.US&from_date=bad&to_date=2026-06-01")

        assert bad_format.status_code == 400
        assert bad_date.status_code == 400
```

- [ ] **Step 2: Run API tests and inspect failures**

Run from `backend/`:

```bash
python3 -m pytest tests/test_reports_api.py -v
```

Expected before fixes: failures if router/schema is not fully wired or response serialization is incomplete.

- [ ] **Step 3: Harden date range validation**

In `backend/app/services/report_service.py`, update `get_range_report` after parsing dates:

```python
        if to_d < from_d:
            raise ValueError("to_date must be greater than or equal to from_date")
```

- [ ] **Step 4: Run API tests again**

Run from `backend/`:

```bash
python3 -m pytest tests/test_reports_api.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Run combined backend report tests**

Run from `backend/`:

```bash
python3 -m pytest tests/test_report_service.py tests/test_reports_api.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Checkpoint**

Run from repo root:

```bash
git diff -- backend/app/api/reports.py backend/app/services/report_service.py backend/tests/test_reports_api.py
```

Expected: diff is limited to report API hardening and tests.

---

### Task 4: Frontend types, route, navigation, and API stubs

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/cypress/support/e2e.ts`

- [ ] **Step 1: Add report TypeScript interfaces**

Append these interfaces to `frontend/src/types/index.ts`:

```ts
export interface ReportMetrics {
  total_pnl: number
  total_trades: number
  win_count: number
  loss_count: number
  win_rate: number
  profit_loss_ratio: number
  avg_pnl_per_trade: number
  max_profit: number
  max_loss: number
  max_drawdown: number
  llm_suggestions_count: number
  llm_applied_count: number
  llm_apply_rate: number
  llm_profitable_count: number
  llm_accuracy_rate: number
}

export interface ReportDailyPoint {
  date: string
  pnl: number
  cumulative_pnl: number
  drawdown: number
  trade_count: number
  win_count: number
}

export interface ReportAttributionPoint {
  key: string
  label: string
  trade_count: number
  pnl: number
  win_rate: number
  share: number
}

export interface ReportOrderDetail {
  broker_order_id: string
  side: string
  quantity: number
  executed_price: number
  status: string
  filled_at: string | null
  pnl: number
}

export interface ReportDayDetail {
  date: string
  orders: ReportOrderDetail[]
}

export interface ReportResponse {
  period_type: string
  symbol: string
  start_date: string
  end_date: string
  metrics: ReportMetrics
  daily_points: ReportDailyPoint[]
  attribution: ReportAttributionPoint[]
  details: ReportDayDetail[]
}
```

- [ ] **Step 2: Add route**

In `frontend/src/router/index.ts`, add this route before the catch-all route:

```ts
  { path: '/reports', component: () => import('../views/Reports.vue') },
```

- [ ] **Step 3: Add desktop navigation link**

In `frontend/src/App.vue`, add this link after the Review link:

```vue
        <router-link to="/reports" class="app-menu-link" :class="{ active: route.path === '/reports' }">交易报告</router-link>
```

Do not add it to mobile bottom nav in Phase 1; the existing bottom nav is already crowded.

- [ ] **Step 4: Add reports stubs to Cypress support**

In `frontend/cypress/support/e2e.ts`, inside `cy.stubApi()`, add:

```ts
  cy.intercept('GET', '/api/reports/range*', {
    body: {
      period_type: 'range',
      symbol: 'AAPL.US',
      start_date: '2026-06-01',
      end_date: '2026-06-30',
      metrics: {
        total_pnl: 0,
        total_trades: 0,
        win_count: 0,
        loss_count: 0,
        win_rate: 0,
        profit_loss_ratio: 0,
        avg_pnl_per_trade: 0,
        max_profit: 0,
        max_loss: 0,
        max_drawdown: 0,
        llm_suggestions_count: 0,
        llm_applied_count: 0,
        llm_apply_rate: 0,
        llm_profitable_count: 0,
        llm_accuracy_rate: 0,
      },
      daily_points: [],
      attribution: [],
      details: [],
    },
  }).as('getReport')

  cy.intercept('GET', '/api/reports/export*', {
    body: new Blob(['date,symbol,pnl\n'], { type: 'text/csv' }),
  }).as('exportReport')
```

- [ ] **Step 5: Run type check**

Run from `frontend/`:

```bash
npm run type-check
```

Expected: if `Reports.vue` still references missing fields or invalid types, type-check fails before Task 5.

- [ ] **Step 6: Checkpoint**

Run from repo root:

```bash
git diff -- frontend/src/types/index.ts frontend/src/router/index.ts frontend/src/App.vue frontend/cypress/support/e2e.ts
```

Expected: diff contains only report type, route, nav, and stub additions.

---

### Task 5: Reports.vue Phase 1 UX and chart data

**Files:**
- Modify: `frontend/src/views/Reports.vue`
- Modify if needed: `frontend/src/api/reports.ts`

- [ ] **Step 1: Update default date range and validation helpers**

In `frontend/src/views/Reports.vue`, replace the initial form setup with:

```ts
function formatDate(date: Date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function daysAgo(days: number) {
  const date = new Date()
  date.setDate(date.getDate() - days)
  return date
}

const form = ref({
  symbol: 'AAPL.US',
  from_date: formatDate(daysAgo(30)),
  to_date: formatDate(new Date()),
})
```

Then add this validation helper before `handleSearch()`:

```ts
function validateForm() {
  if (!form.value.symbol || !form.value.from_date || !form.value.to_date) {
    ElMessage.warning('请填写完整的查询条件')
    return false
  }
  if (form.value.from_date > form.value.to_date) {
    ElMessage.warning('开始日期不能晚于结束日期')
    return false
  }
  return true
}
```

- [ ] **Step 2: Update `handleSearch` to use validation**

Replace the first guard in `handleSearch()` with:

```ts
  if (!validateForm()) {
    return
  }
```

- [ ] **Step 3: Add max drawdown metric card**

Replace one of the second-row LLM-only cards or add a fifth responsive card with:

```vue
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value negative">{{ reportData.metrics.max_drawdown.toFixed(2) }}</div>
            <div class="summary-label">最大回撤</div>
          </el-card>
        </el-col>
```

Keep LLM apply/accuracy visible by combining them in one card if needed:

```vue
            <div class="summary-value">{{ (reportData.metrics.llm_apply_rate * 100).toFixed(1) }}% / {{ (reportData.metrics.llm_accuracy_rate * 100).toFixed(1) }}%</div>
            <div class="summary-label">LLM 采纳 / 准确</div>
```

- [ ] **Step 4: Add cumulative line data**

Add this computed after `bars`:

```ts
const cumulativeRange = computed(() => {
  if (chartData.value.length === 0) return { min: 0, max: 1 }
  const values = chartData.value.map(point => point.cumulative_pnl)
  const min = Math.min(...values, 0)
  const max = Math.max(...values, 1)
  return min === max ? { min: min - 1, max: max + 1 } : { min, max }
})

const cumulativeLinePoints = computed(() => {
  const data = chartData.value
  if (data.length === 0) return ''
  const plotWidth = chartWidth - padding.left - padding.right
  const plotHeight = chartHeight - padding.top - padding.bottom
  const spacing = data.length > 1 ? plotWidth / (data.length - 1) : plotWidth
  const range = cumulativeRange.value.max - cumulativeRange.value.min

  return data.map((point, idx) => {
    const x = data.length > 1 ? padding.left + idx * spacing : padding.left + plotWidth / 2
    const y = padding.top + (1 - (point.cumulative_pnl - cumulativeRange.value.min) / range) * plotHeight
    return `${x},${y}`
  }).join(' ')
})
```

- [ ] **Step 5: Render cumulative line in SVG**

Inside the existing SVG after the bars, add:

```vue
            <polyline v-if="cumulativeLinePoints"
              :points="cumulativeLinePoints"
              fill="none"
              stroke="#2563eb"
              stroke-width="2" />
```

- [ ] **Step 6: Update daily table columns**

Add columns for cumulative PnL and drawdown:

```vue
          <el-table-column label="累计盈亏" width="120">
            <template #default="{ row }">
              <span :class="row.cumulative_pnl >= 0 ? 'positive' : 'negative'">{{ signedCurrency(row.cumulative_pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="回撤" width="120">
            <template #default="{ row }">
              <span class="negative">{{ row.drawdown.toFixed(2) }}</span>
            </template>
          </el-table-column>
```

- [ ] **Step 7: Run type check**

Run from `frontend/`:

```bash
npm run type-check
```

Expected: type-check passes.

- [ ] **Step 8: Checkpoint**

Run from repo root:

```bash
git diff -- frontend/src/views/Reports.vue frontend/src/api/reports.ts
```

Expected: diff contains only Reports Phase 1 UX/chart changes.

---

### Task 6: Cypress Reports coverage

**Files:**
- Modify: `frontend/cypress/e2e/reports.cy.ts`

- [ ] **Step 1: Update mocked response shape**

In all mocked report bodies in `frontend/cypress/e2e/reports.cy.ts`, add:

```ts
max_drawdown: 50,
```

inside `metrics`, add `cumulative_pnl` and `drawdown` to each `daily_points` item, and add:

```ts
attribution: [],
details: [],
```

at the response top level.

- [ ] **Step 2: Add validation test**

Add this test:

```ts
  it('should validate required fields and date order', () => {
    cy.visitApp('/reports')
    cy.get('[data-testid="reports-view"]').should('be.visible')
    cy.get('input[placeholder*="例如 AAPL.US"]').clear()
    cy.contains('查询').click()
    cy.contains('请填写完整的查询条件').should('be.visible')

    cy.get('input[placeholder*="例如 AAPL.US"]').type('AAPL.US')
    cy.get('input').eq(1).clear().type('2026-06-30')
    cy.get('input').eq(2).clear().type('2026-06-01')
    cy.contains('查询').click()
    cy.contains('开始日期不能晚于结束日期').should('be.visible')
  })
```

- [ ] **Step 3: Add chart/export assertions to data test**

In the existing “display report data with metrics” test, add assertions:

```ts
    cy.contains('最大回撤').should('be.visible')
    cy.contains('50.00').should('be.visible')
    cy.get('svg.pnl-chart polyline').should('exist')
    cy.contains('累计盈亏').should('be.visible')
    cy.contains('回撤').should('be.visible')
```

Add a separate export test:

```ts
  it('should request JSON and CSV exports', () => {
    cy.intercept('GET', '/api/reports/range*', {
      statusCode: 200,
      body: {
        period_type: 'range',
        symbol: 'AAPL.US',
        start_date: '2026-06-01',
        end_date: '2026-06-01',
        metrics: {
          total_pnl: 10,
          total_trades: 1,
          win_count: 1,
          loss_count: 0,
          win_rate: 1,
          profit_loss_ratio: 0,
          avg_pnl_per_trade: 10,
          max_profit: 10,
          max_loss: 0,
          max_drawdown: 0,
          llm_suggestions_count: 0,
          llm_applied_count: 0,
          llm_apply_rate: 0,
          llm_profitable_count: 0,
          llm_accuracy_rate: 0,
        },
        daily_points: [{ date: '2026-06-01', pnl: 10, cumulative_pnl: 10, drawdown: 0, trade_count: 1, win_count: 1 }],
        attribution: [],
        details: [],
      },
    }).as('getReportForExport')
    cy.intercept('GET', '/api/reports/export*format=json*', { body: {} }).as('exportJson')
    cy.intercept('GET', '/api/reports/export*format=csv*', { body: 'date,symbol,pnl\n' }).as('exportCsv')

    cy.visitApp('/reports')
    cy.contains('查询').click()
    cy.wait('@getReportForExport')
    cy.contains('导出 JSON').click()
    cy.wait('@exportJson')
    cy.contains('导出 CSV').click()
    cy.wait('@exportCsv')
  })
```

- [ ] **Step 4: Run Reports Cypress spec**

Run from `frontend/`:

```bash
npm run cypress:run -- --spec cypress/e2e/reports.cy.ts
```

Expected: all Reports Cypress tests pass.

- [ ] **Step 5: Checkpoint**

Run from repo root:

```bash
git diff -- frontend/cypress/e2e/reports.cy.ts
```

Expected: diff contains only report E2E updates.

---

### Task 7: Final verification and integration check

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run backend report tests**

Run from `backend/`:

```bash
python3 -m pytest tests/test_report_service.py tests/test_reports_api.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Run backend type checker**

Run from `backend/`:

```bash
python3 -m basedpyright
```

Expected: `0 errors, 0 warnings, 0 notes` or no new report-related errors if local config differs.

- [ ] **Step 3: Run frontend type check**

Run from `frontend/`:

```bash
npm run type-check
```

Expected: type-check passes.

- [ ] **Step 4: Run Reports Cypress spec**

Run from `frontend/`:

```bash
npm run cypress:run -- --spec cypress/e2e/reports.cy.ts
```

Expected: spec passes.

- [ ] **Step 5: Optional production build check**

Run from `frontend/`:

```bash
npm run build
```

Expected: `vue-tsc` and Vite build pass.

- [ ] **Step 6: Inspect final diff**

Run from repo root:

```bash
git status --short
git diff --stat
```

Expected: modified/untracked files are limited to Reports Phase 1 implementation, tests, and the already-created spec/plan docs.

---

## Self-Review

- Spec coverage: This plan covers Phase 1 fully: backend connection, schemas, metrics, cumulative PnL, drawdown, export, frontend route/nav/types/page, Cypress, and verification. Phase 2/3 are intentionally not implemented; their response arrays are pre-wired as empty arrays.
- Placeholder scan: No `TBD`, `TODO`, or “implement later” instructions remain. Phase 2/3 are explicitly out of this Phase 1 plan.
- Type consistency: Backend `ReportResponse` fields match frontend `ReportResponse` fields and `ReportService._report_to_dict` keys.
