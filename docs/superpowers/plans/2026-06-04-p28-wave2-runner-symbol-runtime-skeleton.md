# P28 Wave 2 Runner Symbol Runtime Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a symbol-keyed runtime skeleton inside `AppRunner` so quotes and engine state can be tracked per symbol while automatic trading stays single-primary.

**Architecture:** Keep `self.engine`, `/api/status`, `RiskController`, and automatic order triggers bound to the primary `StrategyConfig.symbol`. Add a lightweight `SymbolRuntime` dataclass and `AppRunner._symbol_runtimes: dict[str, SymbolRuntime]`, populated from the watchlist plus primary strategy symbol. `_on_quote()` records quotes into the matching symbol runtime, but only the primary symbol is allowed to drive `self.engine.update_price()` and order execution.

**Tech Stack:** Python 3.11, pytest, basedpyright, existing `AppRunner` sync/threading patterns.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/runner.py` | Define `SymbolRuntime`, load watchlist symbols into `_symbol_runtimes`, and route non-primary quotes without triggering trades. | Modify |
| `backend/tests/test_runner.py` | Cover runtime loading and secondary-quote non-trigger behavior. | Modify |
| `docs/Roadmap.md` | Mark P28 Wave 2 complete and update next iteration plan. | Modify |

---

## Task 1: Runner runtime skeleton tests

**Files:**
- Modify: `backend/tests/test_runner.py`

- [x] **Step 1: Add failing tests**

Add these tests inside `class TestAppRunner`:

```python
    def test_sync_symbol_runtimes_loads_watchlist_without_replacing_primary_engine(self, monkeypatch) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=100.0, sell_high=200.0)

        class FakeWatchlistService:
            def __init__(self, db) -> None:
                pass

            def list_items(self):
                return [
                    SimpleNamespace(symbol="AAPL.US", market="US"),
                    SimpleNamespace(symbol="NVDA.US", market="US"),
                ]

        monkeypatch.setattr("app.runner.WatchlistService", FakeWatchlistService)

        runner._sync_symbol_runtimes(object())

        assert set(runner._symbol_runtimes) == {"NVDA.US", "AAPL.US"}
        assert runner._symbol_runtimes["NVDA.US"].engine is runner.engine
        assert runner._symbol_runtimes["AAPL.US"].engine is not runner.engine
        assert runner._symbol_runtimes["AAPL.US"].engine.params.symbol == "AAPL.US"
        assert runner.engine.params.symbol == "NVDA.US"

    def test_secondary_quote_updates_runtime_without_triggering_primary_engine(self, monkeypatch) -> None:
        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=100.0, sell_high=200.0)

        class FakeWatchlistService:
            def __init__(self, db) -> None:
                pass

            def list_items(self):
                return [SimpleNamespace(symbol="AAPL.US", market="US")]

        monkeypatch.setattr("app.runner.WatchlistService", FakeWatchlistService)
        runner._sync_symbol_runtimes(object())

        runner._on_quote(Quote("AAPL.US", 50.0, 49.9, 50.1, ""))

        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.last_price == 0.0
        secondary = runner._symbol_runtimes["AAPL.US"]
        assert secondary.engine.last_price == 50.0
        assert len(secondary.recent_quotes) == 1
        assert secondary.recent_quotes[0]["symbol"] == "AAPL.US"
```

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
cd backend && ./.venv/bin/python -m pytest tests/test_runner.py::TestAppRunner::test_sync_symbol_runtimes_loads_watchlist_without_replacing_primary_engine tests/test_runner.py::TestAppRunner::test_secondary_quote_updates_runtime_without_triggering_primary_engine -v
```

Expected: fail because `_sync_symbol_runtimes` / `_symbol_runtimes` do not exist yet.

---

## Task 2: Symbol runtime implementation

**Files:**
- Modify: `backend/app/runner.py`

- [x] **Step 1: Add imports and dataclass**

Change imports:

```python
from dataclasses import dataclass, replace as dataclass_replace
```

Add after constants:

```python
@dataclass
class SymbolRuntime:
    symbol: str
    market: str
    engine: StrategyEngine
    recent_quotes: list[dict[str, Any]]
```

- [x] **Step 2: Initialize runtime dict**

In `AppRunner.__init__`, after `self.engine = StrategyEngine()` add:

```python
        self._symbol_runtimes: dict[str, SymbolRuntime] = {}
```

- [x] **Step 3: Import watchlist service**

Add import:

```python
from app.services.watchlist_service import WatchlistService
```

- [x] **Step 4: Add runtime helpers**

Add methods near `_refresh_trading_session_mode()`:

```python
    def _build_symbol_runtime(self, symbol: str, market: str, *, primary: bool = False) -> SymbolRuntime:
        engine = self.engine if primary else StrategyEngine(StrategyParams(symbol=symbol, market=market))
        return SymbolRuntime(symbol=symbol, market=market, engine=engine, recent_quotes=[])

    def _sync_symbol_runtimes(self, db: Session) -> None:
        primary_symbol = self.engine.params.symbol
        symbol_markets: dict[str, str] = {}
        if primary_symbol:
            symbol_markets[primary_symbol] = self.engine.params.market
        for item in WatchlistService(db).list_items():
            symbol = getattr(item, "symbol", "")
            if not symbol:
                continue
            symbol_markets[symbol] = getattr(item, "market", "US") or "US"

        with self._state_lock:
            for symbol, market in symbol_markets.items():
                runtime = self._symbol_runtimes.get(symbol)
                if runtime is None:
                    self._symbol_runtimes[symbol] = self._build_symbol_runtime(
                        symbol,
                        market,
                        primary=symbol == primary_symbol,
                    )
                    continue
                runtime.market = market
                runtime.engine.params.market = market
                if symbol == primary_symbol:
                    runtime.engine = self.engine
            for symbol in list(self._symbol_runtimes):
                if symbol not in symbol_markets:
                    del self._symbol_runtimes[symbol]
```

- [x] **Step 5: Populate runtimes during runner initialization and strategy reload**

In `_initialize_runner()`, after `_load_tracked_entries(db)` add:

```python
            self._sync_symbol_runtimes(db)
```

In `reload_strategy()`, after assigning `self._trade_svc.margin_safety_factor`, add:

```python
                self._sync_symbol_runtimes(db)
```

- [x] **Step 6: Add per-runtime quote recording**

Add helper near `_remember_quote()`:

```python
    def _remember_symbol_runtime_quote(self, quote: Quote, observed_at: datetime) -> None:
        runtime = self._symbol_runtimes.get(quote.symbol)
        if runtime is None:
            runtime = self._build_symbol_runtime(quote.symbol, self.engine.params.market)
            self._symbol_runtimes[quote.symbol] = runtime
        runtime.engine.record_price(quote.last_price)
        runtime.recent_quotes.append(
            {
                "symbol": quote.symbol,
                "last_price": float(quote.last_price),
                "bid": float(quote.bid),
                "ask": float(quote.ask),
                "timestamp": quote.timestamp,
                "observed_at": observed_at,
            }
        )
        cutoff = observed_at - timedelta(seconds=self._recent_quote_window_seconds)
        runtime.recent_quotes = [
            item
            for item in runtime.recent_quotes
            if isinstance(item.get("observed_at"), datetime) and item["observed_at"] >= cutoff
        ]
        if len(runtime.recent_quotes) > self._recent_quotes_cap:
            runtime.recent_quotes = runtime.recent_quotes[-self._recent_quotes_cap:]
```

At the start of `_remember_quote()`, after `now = datetime.now(timezone.utc)`, call:

```python
        self._remember_symbol_runtime_quote(quote, now)
```

- [x] **Step 7: Block secondary quotes from triggering primary engine**

In `_on_quote()`, after `_remember_quote(quote)` add:

```python
                if quote.symbol != self.engine.params.symbol:
                    return
```

- [x] **Step 8: Run focused tests and verify GREEN**

Run:

```bash
cd backend && ./.venv/bin/python -m pytest tests/test_runner.py::TestAppRunner::test_sync_symbol_runtimes_loads_watchlist_without_replacing_primary_engine tests/test_runner.py::TestAppRunner::test_secondary_quote_updates_runtime_without_triggering_primary_engine -v
```

Expected: 2 passed.

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] **Step 1: Run runner tests**

Run:

```bash
cd backend && ./.venv/bin/python -m pytest tests/test_runner.py -v
```

Expected: all tests in `test_runner.py` pass.

- [x] **Step 2: Run backend type-check**

Run:

```bash
cd backend && ./.venv/bin/basedpyright
```

Expected: 0 errors / 0 warnings / 0 notes.

- [x] **Step 3: Update roadmap**

Add a completed row for P28 Wave 2 and shift next iteration plan to P28 Wave 3:

```markdown
| 已完成 | **P28 Wave 2：Runner 多标的 runtime 状态骨架** | 已完成 2026-06-04 | 新增 `SymbolRuntime` 与 `AppRunner._symbol_runtimes`，从 Watchlist + 当前策略标的加载 symbol runtime；quote 会写入对应 runtime 的 engine / recent_quotes；非主交易标的 quote 不触发 `self.engine.update_price()` 或下单，保持 `/api/status` 与自动交易路径单主标的兼容。验证：focused runner tests 2 passed / `test_runner.py` passed / basedpyright 0/0/0。 |
```

---

## Self-review

### Spec coverage

- P28 Wave 2 要求 runner runtime skeleton：Task 2 覆盖。
- 保持自动交易单主标的：Task 1 secondary quote test 覆盖。
- 不做 RuntimeState schema：本计划不修改 DB model/migration。
- 不做多标的下单启用：本计划不修改 order action policy。

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。
- 所有代码步骤包含具体代码块。
- 所有验证步骤包含精确命令和预期。

### Type consistency

- Dataclass: `SymbolRuntime`。
- Runtime dict: `AppRunner._symbol_runtimes: dict[str, SymbolRuntime]`。
- Runtime sync method: `_sync_symbol_runtimes(db: Session) -> None`。
