# LLM Prompt Engineering Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过模块化 prompt 架构、技术指标扩展和 A/B 测试支持，提升 LLM 交易顾问的决策质量和系统性能。

**Architecture:** 简化 DDD 分层架构。领域层包含 PromptBuilder、TechnicalAnalyzer、RiskAssessor；应用层编排 LLM 分析流程；接口层暴露 API。现有 `DataAggregator.build_prompt()` 将被重构为模块化组合。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / SQLite / pytest / httpx

**Baseline (2026-05-28):** `pytest 493 passed`，`basedpyright` 0 errors / 0 warnings。

**Estimated Effort:** 2 周（Phase 1: 1 周，Phase 2: 1 周）

---

## File Structure

### Phase 1 — 模块化 Prompt + 技术指标 + A/B 测试

| File | Responsibility |
|------|----------------|
| `backend/app/domain/__init__.py` | Domain layer package |
| `backend/app/domain/prompt/__init__.py` | Prompt module package |
| `backend/app/domain/prompt/base.py` | `PromptModule` abstract base class |
| `backend/app/domain/prompt/system_module.py` | System instructions (role, rules) |
| `backend/app/domain/prompt/context_module.py` | Market data, candles, indicators |
| `backend/app/domain/prompt/strategy_module.py` | Strategy params, position, risk rules |
| `backend/app/domain/prompt/output_module.py` | Output JSON format + constraints |
| `backend/app/domain/prompt/prompt_builder.py` | `PromptBuilder` orchestrator |
| `backend/app/domain/analysis/__init__.py` | Analysis package |
| `backend/app/domain/analysis/technical_indicators.py` | RSI, MACD, volume analysis |
| `backend/app/domain/experiment/__init__.py` | Experiment package |
| `backend/app/domain/experiment/prompt_version.py` | `PromptVersion` domain model |
| `backend/app/domain/experiment/ab_test_manager.py` | A/B test selection + recording |
| `backend/app/models.py` | Add `PromptVersion`, `ExperimentResult` tables |
| `backend/app/database.py` | Add `_ensure_prompt_versions_table`, `_ensure_experiment_results_table` |
| `backend/app/schemas.py` | Add experiment API schemas |
| `backend/app/api/experiments.py` | Experiment CRUD + results API |
| `backend/app/main.py` | Mount experiments router |
| `backend/app/services/data_aggregator.py` | Refactor to use `PromptBuilder` + new indicators |
| `backend/app/services/llm_advisor_service.py` | Use `PromptBuilder`, support experiment variant selection |
| `backend/tests/test_prompt_builder.py` | PromptBuilder unit tests |
| `backend/tests/test_technical_indicators.py` | RSI, MACD, volume tests |
| `backend/tests/test_ab_test_manager.py` | A/B test manager tests |
| `backend/tests/test_data_aggregator.py` | Update existing tests for new prompt format |
| `backend/tests/test_llm_advisor.py` | Update for PromptBuilder integration |

### Phase 2 — 市场情绪 + 多时间框架 + 性能追踪

| File | Responsibility |
|------|----------------|
| `backend/app/domain/sentiment/__init__.py` | Sentiment package |
| `backend/app/domain/sentiment/news_sentiment.py` | News sentiment placeholder (stub) |
| `backend/app/domain/sentiment/market_sentiment.py` | Market fear/greed indicators |
| `backend/app/domain/prompt/sentiment_module.py` | Sentiment data in prompt |
| `backend/app/domain/performance/__init__.py` | Performance package |
| `backend/app/domain/performance/performance_tracker.py` | Track LLM prediction accuracy |
| `backend/app/domain/performance/result_evaluator.py` | Evaluate trade outcomes |
| `backend/app/schemas.py` | Add performance API schemas |
| `backend/app/api/performance.py` | Performance query API |
| `backend/app/main.py` | Mount performance router |
| `backend/tests/test_sentiment.py` | Sentiment module tests |
| `backend/tests/test_performance_tracker.py` | Performance tracking tests |

---

## Phase 1: 模块化 Prompt + 技术指标 + A/B 测试

### Task 1: PromptModule 基类与 SystemModule

**Files:**
- Create: `backend/app/domain/__init__.py`
- Create: `backend/app/domain/prompt/__init__.py`
- Create: `backend/app/domain/prompt/base.py`
- Create: `backend/app/domain/prompt/system_module.py`
- Test: `backend/tests/test_prompt_builder.py`

- [ ] **Step 1: Create domain package init files**

```python
# backend/app/domain/__init__.py
```

```python
# backend/app/domain/prompt/__init__.py
```

- [ ] **Step 2: Write failing test for PromptModule base class**

```python
# backend/tests/test_prompt_builder.py
from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_prompt_builder.db",
)

import pytest
from app.domain.prompt.base import PromptModule
from app.domain.prompt.system_module import SystemModule


class TestPromptModule:
    def test_base_class_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            PromptModule()  # type: ignore[abstract]

    def test_system_module_renders_role(self) -> None:
        module = SystemModule()
        result = module.render({})
        assert "量化交易顾问" in result
        assert len(result) > 10
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py -v`
Expected: FAIL with `ModuleNotFoundError` or `ImportError`

- [ ] **Step 4: Implement PromptModule base class**

```python
# backend/app/domain/prompt/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PromptModule(ABC):
    """Abstract base class for prompt modules."""

    @abstractmethod
    def render(self, context: dict[str, Any]) -> str:
        """Render this module's section of the prompt."""
        ...
```

- [ ] **Step 5: Implement SystemModule**

```python
# backend/app/domain/prompt/system_module.py
from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class SystemModule(PromptModule):
    """Renders the system role and base instructions."""

    def render(self, context: dict[str, Any]) -> str:
        return (
            "你是一个专业量化交易顾问。请基于以下市场数据、账户购买力、持仓成本和最近5分钟累次报价，"
            "为区间交易策略推荐买入下限（buy_low）和卖出上限（sell_high），"
            "并在信号特别明确时给出即时订单动作。"
        )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Commit**

```bash
cd backend && git add app/domain/ tests/test_prompt_builder.py
git commit -m "feat(prompt): add PromptModule base class and SystemModule"
```

---

### Task 2: ContextModule — 市场数据与技术指标渲染

**Files:**
- Create: `backend/app/domain/prompt/context_module.py`
- Modify: `backend/tests/test_prompt_builder.py`

- [ ] **Step 1: Write failing tests for ContextModule**

Append to `backend/tests/test_prompt_builder.py`:

```python
from app.domain.prompt.context_module import ContextModule


class TestContextModule:
    def test_renders_daily_candle_table(self) -> None:
        module = ContextModule()
        context = {
            "symbol": "AAPL.US",
            "market": "US",
            "current_price": 200.0,
            "daily_candles": [
                {"date": "2026-05-20", "open": 195.0, "high": 202.0, "low": 194.0, "close": 200.0, "volume": 50000},
                {"date": "2026-05-21", "open": 200.0, "high": 205.0, "low": 199.0, "close": 203.0, "volume": 60000},
            ],
            "minute_candles": [],
            "atr": 3.5,
            "bb_upper": 210.0,
            "bb_middle": 200.0,
            "bb_lower": 190.0,
            "rsi": 55.0,
            "macd": {"macd": 1.2, "signal": 0.8, "histogram": 0.4},
            "volume_analysis": {"avg_volume": 55000.0, "volume_ratio": 1.1, "trend": "normal"},
        }
        result = module.render(context)
        assert "2026-05-20" in result
        assert "200.00" in result
        assert "ATR" in result

    def test_renders_placeholder_when_no_candles(self) -> None:
        module = ContextModule()
        context = {
            "symbol": "AAPL.US",
            "market": "US",
            "current_price": 0.0,
            "daily_candles": [],
            "minute_candles": [],
            "atr": 0.0,
            "bb_upper": 0.0,
            "bb_middle": 0.0,
            "bb_lower": 0.0,
            "rsi": 0.0,
            "macd": {"macd": 0.0, "signal": 0.0, "histogram": 0.0},
            "volume_analysis": {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"},
        }
        result = module.render(context)
        assert "暂无可用历史日 K 数据" in result

    def test_renders_rsi_macd_when_present(self) -> None:
        module = ContextModule()
        context = {
            "symbol": "TSLA.US",
            "market": "US",
            "current_price": 250.0,
            "daily_candles": [{"date": "2026-05-21", "open": 248.0, "high": 255.0, "low": 247.0, "close": 252.0, "volume": 80000}],
            "minute_candles": [],
            "atr": 5.0,
            "bb_upper": 260.0,
            "bb_middle": 250.0,
            "bb_lower": 240.0,
            "rsi": 62.5,
            "macd": {"macd": 2.1, "signal": 1.5, "histogram": 0.6},
            "volume_analysis": {"avg_volume": 75000.0, "volume_ratio": 1.07, "trend": "normal"},
        }
        result = module.render(context)
        assert "RSI" in result
        assert "62.50" in result
        assert "MACD" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py::TestContextModule -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement ContextModule**

```python
# backend/app/domain/prompt/context_module.py
from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule

_PROMPT_DAILY_CANDLES = 7
_PROMPT_MINUTE_CANDLES = 30


def _format_optional_price(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _render_daily_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "（暂无可用历史日 K 数据）"
    table = "| 日期 | 开盘 | 最高 | 最低 | 收盘 | 成交量 |\n|------|------|------|------|------|--------|"
    for c in rows:
        table += (
            f"\n| {c.get('date', '-')} "
            f"| {float(c.get('open', 0)):.2f} "
            f"| {float(c.get('high', 0)):.2f} "
            f"| {float(c.get('low', 0)):.2f} "
            f"| {float(c.get('close', 0)):.2f} "
            f"| {int(c.get('volume', 0))} |"
        )
    return table


def _render_minute_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "（暂无可用 1 分钟 K 数据）"
    table = "| 时间 | 开盘 | 最高 | 最低 | 收盘 | 成交量 |\n|------|------|------|------|------|--------|"
    for c in rows:
        table += (
            f"\n| {c.get('time', '-')} "
            f"| {float(c.get('open', 0)):.2f} "
            f"| {float(c.get('high', 0)):.2f} "
            f"| {float(c.get('low', 0)):.2f} "
            f"| {float(c.get('close', 0)):.2f} "
            f"| {int(c.get('volume', 0))} |"
        )
    return table


class ContextModule(PromptModule):
    """Renders market data: candle tables, technical indicators, current price."""

    def render(self, context: dict[str, Any]) -> str:
        daily_candles = context.get("daily_candles", [])
        minute_candles = context.get("minute_candles", [])
        ohlcv_table = _render_daily_table(daily_candles[-_PROMPT_DAILY_CANDLES:])
        minute_table = _render_minute_table(minute_candles[-_PROMPT_MINUTE_CANDLES:])

        atr = context.get("atr", 0.0)
        bb_upper = context.get("bb_upper", 0.0)
        bb_middle = context.get("bb_middle", 0.0)
        bb_lower = context.get("bb_lower", 0.0)
        current_price = context.get("current_price", 0.0)

        rsi = context.get("rsi", 0.0)
        macd = context.get("macd", {})
        volume_analysis = context.get("volume_analysis", {})

        lines = [
            "## 市场数据（最近日 K 线）",
            ohlcv_table,
            "",
            "## 市场数据（最近 1 分钟 K 线）",
            minute_table,
            "",
            "## 当前技术指标",
            f"- ATR(14): {atr:.2f}",
            f"- 布林带: 上轨 {bb_upper:.2f} / 中轨 {bb_middle:.2f} / 下轨 {bb_lower:.2f}",
            f"- 当前价格: {current_price:.2f}",
        ]

        if rsi > 0:
            lines.append(f"- RSI(14): {rsi:.2f}")
        if macd and macd.get("macd", 0) != 0:
            lines.append(f"- MACD: {macd['macd']:.2f} / Signal: {macd['signal']:.2f} / Hist: {macd['histogram']:.2f}")
        if volume_analysis and volume_analysis.get("avg_volume", 0) > 0:
            lines.append(
                f"- 成交量: 均量 {volume_analysis['avg_volume']:.0f} / "
                f"量比 {volume_analysis['volume_ratio']:.2f} / {volume_analysis['trend']}"
            )

        return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/domain/prompt/context_module.py tests/test_prompt_builder.py
git commit -m "feat(prompt): add ContextModule with candle tables and indicators"
```

---

### Task 3: StrategyModule — 策略参数与持仓渲染

**Files:**
- Create: `backend/app/domain/prompt/strategy_module.py`
- Modify: `backend/tests/test_prompt_builder.py`

- [ ] **Step 1: Write failing tests for StrategyModule**

Append to `backend/tests/test_prompt_builder.py`:

```python
from app.domain.prompt.strategy_module import StrategyModule


class TestStrategyModule:
    def test_renders_flat_position(self) -> None:
        module = StrategyModule()
        context = {
            "symbol": "AAPL.US",
            "market": "US",
            "current_buy_low": 190.0,
            "current_sell_high": 210.0,
            "short_selling": False,
            "min_profit_amount": 5.0,
            "current_position": "FLAT",
            "position_quantity": 0.0,
            "position_avg_price": 0.0,
            "unrealized_pnl_pct": 0.0,
            "recent_trades": [],
        }
        result = module.render(context)
        assert "FLAT" in result
        assert "190.00" in result
        assert "210.00" in result

    def test_renders_long_position_with_trades(self) -> None:
        module = StrategyModule()
        context = {
            "symbol": "AAPL.US",
            "market": "US",
            "current_buy_low": 195.0,
            "current_sell_high": 210.0,
            "short_selling": False,
            "min_profit_amount": 5.0,
            "current_position": "LONG",
            "position_quantity": 100.0,
            "position_avg_price": 200.0,
            "unrealized_pnl_pct": 2.5,
            "recent_trades": [
                {"side": "BUY", "quantity": 100, "price": 200.0},
            ],
        }
        result = module.render(context)
        assert "LONG" in result
        assert "100" in result
        assert "200.00" in result
        assert "BUY" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py::TestStrategyModule -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement StrategyModule**

```python
# backend/app/domain/prompt/strategy_module.py
from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class StrategyModule(PromptModule):
    """Renders strategy params, position state, and recent trades."""

    def render(self, context: dict[str, Any]) -> str:
        symbol = context.get("symbol", "")
        market = context.get("market", "")
        current_buy_low = context.get("current_buy_low", 0.0)
        current_sell_high = context.get("current_sell_high", 0.0)
        short_selling = context.get("short_selling", False)
        min_profit_amount = context.get("min_profit_amount", 0.0)
        current_position = context.get("current_position", "FLAT")
        position_quantity = context.get("position_quantity", 0.0)
        position_avg_price = context.get("position_avg_price", 0.0)
        unrealized_pnl_pct = context.get("unrealized_pnl_pct", 0.0)
        recent_trades = context.get("recent_trades", [])

        trades_summary = "无"
        if recent_trades:
            trades_summary = "\n".join(
                f"- {t.get('side', '')}: {t.get('quantity', 0)} @ {t.get('price', 0):.2f}"
                for t in recent_trades[:3]
            )

        return f"""## 当前策略参数
- 标的: {symbol} ({market})
- 当前 buy_low: {current_buy_low:.2f}
- 当前 sell_high: {current_sell_high:.2f}
- 允许做空: {short_selling}
- 单笔最低盈利金额: {min_profit_amount:.2f}（约束普通即时卖出/平仓和建议区间宽度；止损动作不受此限制）

## 持仓状态
- 当前持仓方向: {current_position}
- 当前持仓数量: {position_quantity}
- 持仓成本价: {position_avg_price:.2f}
- 浮动盈亏比例: {unrealized_pnl_pct:.2f}%
- 最近成交: {trades_summary}"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/domain/prompt/strategy_module.py tests/test_prompt_builder.py
git commit -m "feat(prompt): add StrategyModule for position and trade rendering"
```

---

### Task 4: OutputModule — 输出格式与约束

**Files:**
- Create: `backend/app/domain/prompt/output_module.py`
- Modify: `backend/tests/test_prompt_builder.py`

- [ ] **Step 1: Write failing test for OutputModule**

Append to `backend/tests/test_prompt_builder.py`:

```python
from app.domain.prompt.output_module import OutputModule


class TestOutputModule:
    def test_renders_json_format(self) -> None:
        module = OutputModule()
        context = {"current_price": 200.0}
        result = module.render(context)
        assert "suggested_buy_low" in result
        assert "suggested_sell_high" in result
        assert "confidence_score" in result
        assert "order_action" in result
        assert "200.00" in result

    def test_includes_all_constraints(self) -> None:
        module = OutputModule()
        context = {"current_price": 150.0}
        result = module.render(context)
        assert "sell_high 必须严格大于 buy_low" in result
        assert "sell_high 必须严格大于当前价格" in result
        assert "buy_low 必须严格小于当前价格" in result
        assert "confidence_score >= 0.7" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py::TestOutputModule -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement OutputModule**

```python
# backend/app/domain/prompt/output_module.py
from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class OutputModule(PromptModule):
    """Renders the expected JSON output format and constraints."""

    def render(self, context: dict[str, Any]) -> str:
        current_price = context.get("current_price", 0.0)

        return f"""## 请输出以下 JSON 格式：
{{
  "analysis": "简短的市场分析（50字以内）",
  "suggested_buy_low": 具体价格,
  "suggested_sell_high": 具体价格,
  "confidence_score": 0.0到1.0,
  "reasoning": "简要推理过程",
  "order_action": "NONE | BUY_NOW | SELL_NOW | SELL_SHORT_NOW | BUY_TO_COVER_NOW | STOP_LOSS_SELL_NOW | STOP_LOSS_COVER_NOW | CANCEL_PENDING | CANCEL_REPLACE",
  "order_price": 具体挂单价格或 null,
  "replacement_action": "NONE | BUY_NOW | SELL_NOW | SELL_SHORT_NOW | BUY_TO_COVER_NOW | STOP_LOSS_SELL_NOW | STOP_LOSS_COVER_NOW",
  "replacement_price": 撤单重挂的新价格或 null,
  "order_reason": "如需立刻交易或撤单重挂，说明原因；否则为空字符串"
}}

注意：
1. sell_high 必须严格大于 buy_low
2. ** sell_high 必须严格大于当前价格 {current_price:.2f}，buy_low 必须严格小于当前价格 {current_price:.2f} **
3. confidence_score >= 0.7 才建议采纳
4. 避免给出与现有持仓方向矛盾的区间
5. 区间宽度应基于 ATR 尽量收窄，促进高频交易
6. FLAT 状态可参考当前价格和 ATR；已有持仓时必须结合持仓成本价、持仓数量和浮动盈亏设计区间，不要仅按当前价格 ±1% 滚动追价
7. LONG 状态下，buy_low 是加仓触发价，应结合成本价和回撤幅度；sell_high 应优先考虑持仓成本价，不要在未说明止损的情况下长期低于成本价
8. 必须综合最近5分钟价格走势、当前价格、持仓成本和最近一次LLM分析结果；如果最新价格已明显偏离旧分析，请说明维持或调整区间的理由
9. 单笔最低盈利金额会约束 suggested_buy_low 与 suggested_sell_high 的区间宽度，也会作为普通 SELL_NOW、BUY_TO_COVER_NOW 的执行门槛，避免手续费成本吞噬收益；止损动作不受此门槛限制
10. 当价格已到达卖出价、需要普通平仓或撤单重挂时，必须在 order_reason 中说明预估收益已覆盖最低盈利门槛；止损信号明确时可以直接给出止损动作
11. 对美股/US 标的，价格波动较快；当信号、购买力和风险收益支持交易时，优先采用"先挂单"策略，不要因为担心价格变化而只给 NONE
12. 若已有当前挂单且你产生了新的即时动作或新价格，按"撤旧单再重挂"的策略输出 CANCEL_REPLACE，并给出 replacement_action 与 replacement_price
13. 必须主动评估止损：若 LONG 持仓下最近5分钟价格连续下破关键支撑、跌幅扩大、买盘无法支撑或出现开始崩盘迹象，应输出 STOP_LOSS_SELL_NOW 及时卖出；若 SHORT 持仓出现相反方向的逼空风险，应输出 STOP_LOSS_COVER_NOW
14. 止损动作允许以控制亏损为优先目标，但必须在 order_reason 中明确写出支撑失效、崩盘、量价恶化或逼空风险等依据
15. 默认 order_action 使用 NONE；只有当最近5分钟累次数据、购买力、持仓成本和风险收益都支持"立即行动"时，才输出 BUY_NOW/SELL_NOW/STOP_LOSS_SELL_NOW 等动作
16. 不允许输出 JSON 以外的解释文本"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/domain/prompt/output_module.py tests/test_prompt_builder.py
git commit -m "feat(prompt): add OutputModule with JSON format and constraints"
```

---

### Task 5: PromptBuilder 编排器

**Files:**
- Create: `backend/app/domain/prompt/prompt_builder.py`
- Modify: `backend/tests/test_prompt_builder.py`

- [ ] **Step 1: Write failing tests for PromptBuilder**

Append to `backend/tests/test_prompt_builder.py`:

```python
from app.domain.prompt.prompt_builder import PromptBuilder
from app.domain.prompt.system_module import SystemModule
from app.domain.prompt.context_module import ContextModule
from app.domain.prompt.strategy_module import StrategyModule
from app.domain.prompt.output_module import OutputModule


class TestPromptBuilder:
    def _full_context(self) -> dict:
        return {
            "symbol": "AAPL.US",
            "market": "US",
            "current_price": 200.0,
            "current_buy_low": 190.0,
            "current_sell_high": 210.0,
            "short_selling": False,
            "min_profit_amount": 5.0,
            "current_position": "FLAT",
            "position_quantity": 0.0,
            "position_avg_price": 0.0,
            "unrealized_pnl_pct": 0.0,
            "recent_trades": [],
            "daily_candles": [
                {"date": "2026-05-20", "open": 195.0, "high": 202.0, "low": 194.0, "close": 200.0, "volume": 50000},
            ],
            "minute_candles": [],
            "atr": 3.5,
            "bb_upper": 210.0,
            "bb_middle": 200.0,
            "bb_lower": 190.0,
            "rsi": 55.0,
            "macd": {"macd": 1.2, "signal": 0.8, "histogram": 0.4},
            "volume_analysis": {"avg_volume": 55000.0, "volume_ratio": 1.1, "trend": "normal"},
            "recent_prices": [],
            "recent_analysis": None,
            "account_context": None,
        }

    def test_build_with_all_modules(self) -> None:
        builder = PromptBuilder()
        builder.add_module(SystemModule())
        builder.add_module(ContextModule())
        builder.add_module(StrategyModule())
        builder.add_module(OutputModule())

        prompt = builder.build(self._full_context())
        assert "量化交易顾问" in prompt
        assert "2026-05-20" in prompt
        assert "FLAT" in prompt
        assert "suggested_buy_low" in prompt

    def test_build_with_no_modules_returns_empty(self) -> None:
        builder = PromptBuilder()
        result = builder.build(self._full_context())
        assert result == ""

    def test_build_preserves_module_order(self) -> None:
        builder = PromptBuilder()
        builder.add_module(SystemModule())
        builder.add_module(OutputModule())

        prompt = builder.build(self._full_context())
        system_pos = prompt.index("量化交易顾问")
        output_pos = prompt.index("suggested_buy_low")
        assert system_pos < output_pos

    def test_modules_are_independent(self) -> None:
        """Each module renders independently; missing keys default gracefully."""
        builder = PromptBuilder()
        builder.add_module(ContextModule())
        builder.add_module(StrategyModule())

        prompt = builder.build({"symbol": "X.US", "market": "US"})
        assert "X.US" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py::TestPromptBuilder -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement PromptBuilder**

```python
# backend/app/domain/prompt/prompt_builder.py
from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class PromptBuilder:
    """Orchestrates modular prompt construction."""

    def __init__(self) -> None:
        self._modules: list[PromptModule] = []

    def add_module(self, module: PromptModule) -> PromptBuilder:
        self._modules.append(module)
        return self

    def build(self, context: dict[str, Any]) -> str:
        parts = []
        for module in self._modules:
            rendered = module.render(context)
            if rendered.strip():
                parts.append(rendered)
        return "\n\n".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/domain/prompt/prompt_builder.py tests/test_prompt_builder.py
git commit -m "feat(prompt): add PromptBuilder orchestrator"
```

---

### Task 6: 技术指标 — RSI、MACD、成交量分析

**Files:**
- Create: `backend/app/domain/__init__.py` (already exists)
- Create: `backend/app/domain/analysis/__init__.py`
- Create: `backend/app/domain/analysis/technical_indicators.py`
- Test: `backend/tests/test_technical_indicators.py`

- [ ] **Step 1: Write failing tests for technical indicators**

```python
# backend/tests/test_technical_indicators.py
from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_tech_indicators.db",
)

import pytest
from app.domain.analysis.technical_indicators import TechnicalIndicators


class TestRSI:
    def test_rsi_with_uptrend(self) -> None:
        # Monotonically increasing prices → RSI should be high
        closes = [100.0 + i for i in range(20)]
        rsi = TechnicalIndicators.calculate_rsi(closes, period=14)
        assert rsi > 70  # overbought territory

    def test_rsi_with_downtrend(self) -> None:
        # Monotonically decreasing prices → RSI should be low
        closes = [200.0 - i for i in range(20)]
        rsi = TechnicalIndicators.calculate_rsi(closes, period=14)
        assert rsi < 30  # oversold territory

    def test_rsi_with_flat_prices(self) -> None:
        closes = [100.0] * 20
        rsi = TechnicalIndicators.calculate_rsi(closes, period=14)
        # Flat prices have no gains or losses; RSI defaults to 50
        assert rsi == 50.0

    def test_rsi_returns_zero_for_insufficient_data(self) -> None:
        closes = [100.0, 101.0]
        rsi = TechnicalIndicators.calculate_rsi(closes, period=14)
        assert rsi == 0.0


class TestMACD:
    def test_macd_returns_three_components(self) -> None:
        closes = [100.0 + i * 0.5 for i in range(50)]
        result = TechnicalIndicators.calculate_macd(closes)
        assert "macd" in result
        assert "signal" in result
        assert "histogram" in result

    def test_macd_histogram_is_difference(self) -> None:
        closes = [100.0 + i * 0.5 for i in range(50)]
        result = TechnicalIndicators.calculate_macd(closes)
        assert result["histogram"] == pytest.approx(result["macd"] - result["signal"], abs=0.01)

    def test_macd_returns_zeros_for_insufficient_data(self) -> None:
        closes = [100.0, 101.0]
        result = TechnicalIndicators.calculate_macd(closes)
        assert result["macd"] == 0.0
        assert result["signal"] == 0.0
        assert result["histogram"] == 0.0


class TestVolumeAnalysis:
    def test_volume_analysis_normal(self) -> None:
        volumes = [50000.0] * 20 + [55000.0]
        result = TechnicalIndicators.analyze_volume(volumes)
        assert result["avg_volume"] > 0
        assert result["volume_ratio"] > 0.9
        assert result["trend"] == "normal"

    def test_volume_analysis_high(self) -> None:
        volumes = [50000.0] * 20 + [150000.0]
        result = TechnicalIndicators.analyze_volume(volumes)
        assert result["volume_ratio"] > 2.0
        assert result["trend"] == "high"

    def test_volume_analysis_low(self) -> None:
        volumes = [50000.0] * 20 + [10000.0]
        result = TechnicalIndicators.analyze_volume(volumes)
        assert result["volume_ratio"] < 0.5
        assert result["trend"] == "low"

    def test_volume_analysis_returns_zeros_for_empty(self) -> None:
        result = TechnicalIndicators.analyze_volume([])
        assert result["avg_volume"] == 0.0
        assert result["volume_ratio"] == 0.0
        assert result["trend"] == "unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement technical indicators**

```python
# backend/app/domain/analysis/__init__.py
```

```python
# backend/app/domain/analysis/technical_indicators.py
from __future__ import annotations

import statistics


class TechnicalIndicators:
    """Compute RSI, MACD, and volume analysis from price/volume series."""

    @staticmethod
    def calculate_rsi(closes: list[float], period: int = 14) -> float:
        """Calculate RSI using the standard smoothed method."""
        if len(closes) < period + 1:
            return 0.0

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0.0 for d in deltas]
        losses = [-d if d < 0 else 0.0 for d in deltas]

        avg_gain = statistics.mean(gains[:period])
        avg_loss = statistics.mean(losses[:period])

        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _ema(values: list[float], period: int) -> list[float]:
        """Compute Exponential Moving Average."""
        if not values:
            return []
        multiplier = 2.0 / (period + 1)
        ema = [values[0]]
        for i in range(1, len(values)):
            ema.append(values[i] * multiplier + ema[-1] * (1 - multiplier))
        return ema

    @classmethod
    def calculate_macd(
        cls,
        closes: list[float],
        fast: int = 12,
        slow: int = 26,
        signal_period: int = 9,
    ) -> dict[str, float]:
        """Calculate MACD line, signal line, and histogram."""
        if len(closes) < slow + signal_period:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}

        ema_fast = cls._ema(closes, fast)
        ema_slow = cls._ema(closes, slow)
        macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal_line = cls._ema(macd_line, signal_period)

        macd_val = macd_line[-1]
        signal_val = signal_line[-1]
        return {
            "macd": macd_val,
            "signal": signal_val,
            "histogram": macd_val - signal_val,
        }

    @staticmethod
    def analyze_volume(volumes: list[float], lookback: int = 20) -> dict[str, float | str]:
        """Analyze volume relative to recent average."""
        if not volumes:
            return {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"}

        recent = volumes[-lookback:] if len(volumes) >= lookback else volumes
        avg_vol = statistics.mean(recent[:-1]) if len(recent) > 1 else recent[0]
        current_vol = recent[-1]

        if avg_vol == 0:
            ratio = 0.0
        else:
            ratio = current_vol / avg_vol

        if ratio > 2.0:
            trend = "high"
        elif ratio < 0.5:
            trend = "low"
        else:
            trend = "normal"

        return {"avg_volume": avg_vol, "volume_ratio": ratio, "trend": trend}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_technical_indicators.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/domain/analysis/ tests/test_technical_indicators.py
git commit -m "feat(analysis): add RSI, MACD, and volume analysis indicators"
```

---

### Task 7: DataAggregator 集成新指标和 PromptBuilder

**Files:**
- Modify: `backend/app/services/data_aggregator.py`
- Modify: `backend/tests/test_data_aggregator.py`

- [ ] **Step 1: Add new indicators to `fetch_market_data`**

In `backend/app/services/data_aggregator.py`, add the import and integrate:

```python
# Add at top of file
from app.domain.analysis.technical_indicators import TechnicalIndicators

# In fetch_market_data, after computing bb_upper/bb_middle/bb_lower, add:
closes = [c.close for c in daily_candles]
volumes = [c.volume for c in daily_candles]
rsi = TechnicalIndicators.calculate_rsi(closes) if len(closes) >= 15 else 0.0
macd = TechnicalIndicators.calculate_macd(closes)
volume_analysis = TechnicalIndicators.analyze_volume(volumes)

# Add to the return dict:
"rsi": rsi,
"macd": macd,
"volume_analysis": volume_analysis,
```

- [ ] **Step 2: Write test for new indicators in fetch_market_data**

Append to `backend/tests/test_data_aggregator.py`:

```python
class TestNewIndicators:
    def test_fetch_market_data_includes_rsi_macd_volume(self) -> None:
        broker = _FakeBroker(
            daily=_build_candles(100.0, 30),
            minute=_build_candles(100.0, 10),
            quote_price=101.5,
        )
        aggregator = DataAggregator(broker=broker)
        result = aggregator.fetch_market_data("AAPL.US", "US")

        assert "rsi" in result
        assert "macd" in result
        assert "volume_analysis" in result
        assert result["rsi"] > 0
        assert "macd" in result["macd"]
        assert "signal" in result["macd"]
        assert result["volume_analysis"]["avg_volume"] > 0
```

- [ ] **Step 3: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_data_aggregator.py -v`
Expected: PASS (all existing + 1 new test)

- [ ] **Step 4: Refactor `build_prompt` to use PromptBuilder**

Replace the `build_prompt` static method in `data_aggregator.py`:

```python
from app.domain.prompt.prompt_builder import PromptBuilder
from app.domain.prompt.system_module import SystemModule
from app.domain.prompt.context_module import ContextModule
from app.domain.prompt.strategy_module import StrategyModule
from app.domain.prompt.output_module import OutputModule

# Replace the existing build_prompt static method with:
@staticmethod
def build_prompt(
    symbol: str,
    market: str,
    current_price: float,
    current_buy_low: float,
    current_sell_high: float,
    short_selling: bool,
    daily_candles: list[dict[str, Any]],
    minute_candles: list[dict[str, Any]],
    atr: float,
    bb_upper: float,
    bb_middle: float,
    bb_lower: float,
    current_position: str,
    recent_trades: list[dict[str, Any]],
    position_quantity: float = 0.0,
    position_avg_price: float = 0.0,
    unrealized_pnl_pct: float = 0.0,
    min_profit_amount: float = 0.0,
    recent_prices: list[dict[str, Any]] | None = None,
    recent_analysis: dict[str, Any] | None = None,
    account_context: dict[str, Any] | None = None,
    rsi: float = 0.0,
    macd: dict[str, float] | None = None,
    volume_analysis: dict[str, Any] | None = None,
) -> str:
    """Build LLM prompt using modular PromptBuilder."""
    context: dict[str, Any] = {
        "symbol": symbol,
        "market": market,
        "current_price": current_price,
        "current_buy_low": current_buy_low,
        "current_sell_high": current_sell_high,
        "short_selling": short_selling,
        "daily_candles": daily_candles,
        "minute_candles": minute_candles,
        "atr": atr,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "current_position": current_position,
        "recent_trades": recent_trades,
        "position_quantity": position_quantity,
        "position_avg_price": position_avg_price,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "min_profit_amount": min_profit_amount,
        "recent_prices": recent_prices,
        "recent_analysis": recent_analysis,
        "account_context": account_context,
        "rsi": rsi,
        "macd": macd or {"macd": 0.0, "signal": 0.0, "histogram": 0.0},
        "volume_analysis": volume_analysis or {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"},
    }

    builder = PromptBuilder()
    builder.add_module(SystemModule())
    builder.add_module(ContextModule())
    builder.add_module(StrategyModule())
    builder.add_module(OutputModule())
    return builder.build(context)
```

- [ ] **Step 5: Update existing prompt tests to pass new parameters**

Update `test_prompt_renders_real_daily_table` and `test_prompt_shows_placeholder_when_no_candles` in `test_data_aggregator.py` to include the new `rsi`, `macd`, `volume_analysis` parameters (they have defaults so existing calls still work, but add explicit values for clarity).

- [ ] **Step 6: Run all tests**

Run: `cd backend && python -m pytest tests/test_data_aggregator.py tests/test_prompt_builder.py tests/test_technical_indicators.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
cd backend && git add app/services/data_aggregator.py tests/test_data_aggregator.py
git commit -m "feat(data): integrate PromptBuilder and new indicators into DataAggregator"
```

---

### Task 8: 数据模型 — PromptVersion 和 ExperimentResult

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/database.py`
- Modify: `backend/app/schemas.py`

- [ ] **Step 1: Add models to `models.py`**

Append to `backend/app/models.py`:

```python
class PromptVersion(Base):
    """Versioned prompt templates for A/B testing."""

    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)


class ExperimentResult(Base):
    """Tracks LLM experiment outcomes for A/B test analysis."""

    __tablename__ = "experiment_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_name: Mapped[str] = mapped_column(String(100), nullable=False)
    variant_name: Mapped[str] = mapped_column(String(100), nullable=False)
    interaction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_action: Mapped[str] = mapped_column(String(32), nullable=False, default="NONE")
    predicted_direction: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    actual_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    was_profitable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)
```

- [ ] **Step 2: Add migration functions to `database.py`**

Add to `backend/app/database.py`:

```python
def _ensure_prompt_versions_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "prompt_versions" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                version VARCHAR(20) NOT NULL,
                description TEXT DEFAULT '' NOT NULL,
                template TEXT NOT NULL,
                is_active BOOLEAN DEFAULT 0 NOT NULL,
                created_at DATETIME
            )
            """
        )


def _ensure_experiment_results_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "experiment_results" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS experiment_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_name VARCHAR(100) NOT NULL,
                variant_name VARCHAR(100) NOT NULL,
                interaction_id INTEGER,
                order_action VARCHAR(32) DEFAULT 'NONE' NOT NULL,
                predicted_direction VARCHAR(10) DEFAULT '' NOT NULL,
                actual_pnl REAL DEFAULT 0.0 NOT NULL,
                was_profitable BOOLEAN,
                created_at DATETIME
            )
            """
        )
```

And call them in `init_db()`:

```python
_ensure_prompt_versions_table(engine)
_ensure_experiment_results_table(engine)
```

- [ ] **Step 3: Add schemas to `schemas.py`**

Append to `backend/app/schemas.py`:

```python
class PromptVersionCreate(BaseModel):
    name: str = Field(max_length=100)
    version: str = Field(max_length=20)
    description: str = Field(default="", max_length=500)
    template: str

class PromptVersionResponse(BaseModel):
    id: int
    name: str
    version: str
    description: str
    template: str
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}

class ExperimentResultResponse(BaseModel):
    id: int
    experiment_name: str
    variant_name: str
    interaction_id: int | None
    order_action: str
    predicted_direction: str
    actual_pnl: float
    was_profitable: bool | None
    created_at: datetime
    model_config = {"from_attributes": True}

class ExperimentSummary(BaseModel):
    experiment_name: str
    variant_name: str
    total_count: int
    profitable_count: int
    avg_pnl: float
    win_rate: float
```

- [ ] **Step 4: Run tests to verify no regressions**

Run: `cd backend && python -m pytest tests/test_database.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/models.py app/database.py app/schemas.py
git commit -m "feat(models): add PromptVersion and ExperimentResult tables"
```

---

### Task 9: A/B 测试管理器

**Files:**
- Create: `backend/app/domain/experiment/__init__.py`
- Create: `backend/app/domain/experiment/ab_test_manager.py`
- Test: `backend/tests/test_ab_test_manager.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_ab_test_manager.py
from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_ab_test.db",
)

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, PromptVersion, ExperimentResult
from app.domain.experiment.ab_test_manager import ABTestManager


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test_ab.db")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


class TestABTestManager:
    def test_create_and_list_versions(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        v1 = manager.create_version("baseline", "1.0", "Original prompt", "template A")
        v2 = manager.create_version("enhanced", "1.1", "With RSI", "template B")

        versions = manager.list_versions()
        assert len(versions) == 2
        assert versions[0].name == "baseline"

    def test_activate_version(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        v1 = manager.create_version("baseline", "1.0", "", "template A")
        v2 = manager.create_version("enhanced", "1.1", "", "template B")

        manager.activate_version(v2.id)
        active = manager.get_active_version()
        assert active is not None
        assert active.name == "enhanced"

        versions = manager.list_versions()
        active_count = sum(1 for v in versions if v.is_active)
        assert active_count == 1

    def test_get_active_returns_none_when_no_active(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        assert manager.get_active_version() is None

    def test_select_variant_for_experiment(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        manager.create_version("baseline", "1.0", "", "template A")
        manager.create_version("enhanced", "1.1", "", "template B")

        # Deterministic selection based on symbol hash
        variant = manager.select_variant("AAPL.US", "prompt_optimization")
        assert variant is not None
        assert variant.name in ("baseline", "enhanced")

    def test_record_result(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        v1 = manager.create_version("baseline", "1.0", "", "template A")

        manager.record_result(
            experiment_name="test_exp",
            variant_name="baseline",
            interaction_id=1,
            order_action="BUY_NOW",
            predicted_direction="UP",
            actual_pnl=50.0,
            was_profitable=True,
        )

        results = db_session.query(ExperimentResult).all()
        assert len(results) == 1
        assert results[0].actual_pnl == 50.0

    def test_get_experiment_summary(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        manager.record_result("exp1", "v1", 1, "BUY_NOW", "UP", 50.0, True)
        manager.record_result("exp1", "v1", 2, "SELL_NOW", "DOWN", -20.0, False)
        manager.record_result("exp1", "v2", 3, "BUY_NOW", "UP", 30.0, True)

        summary = manager.get_experiment_summary("exp1")
        assert len(summary) == 2
        v1_summary = next(s for s in summary if s["variant_name"] == "v1")
        assert v1_summary["total_count"] == 2
        assert v1_summary["win_rate"] == pytest.approx(0.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_ab_test_manager.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement ABTestManager**

```python
# backend/app/domain/experiment/__init__.py
```

```python
# backend/app/domain/experiment/ab_test_manager.py
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from app.models import ExperimentResult, PromptVersion


class ABTestManager:
    """Manages prompt versions and A/B test experiments."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_version(
        self, name: str, version: str, description: str, template: str
    ) -> PromptVersion:
        pv = PromptVersion(
            name=name,
            version=version,
            description=description,
            template=template,
        )
        self.db.add(pv)
        self.db.commit()
        self.db.refresh(pv)
        return pv

    def list_versions(self) -> list[PromptVersion]:
        return (
            self.db.query(PromptVersion)
            .order_by(PromptVersion.id)
            .all()
        )

    def get_active_version(self) -> PromptVersion | None:
        return (
            self.db.query(PromptVersion)
            .filter(PromptVersion.is_active == True)  # noqa: E712
            .first()
        )

    def activate_version(self, version_id: int) -> None:
        self.db.query(PromptVersion).update({PromptVersion.is_active: False})
        version = self.db.get(PromptVersion, version_id)
        if version is None:
            raise ValueError(f"PromptVersion {version_id} not found")
        version.is_active = True
        self.db.commit()

    def select_variant(self, symbol: str, experiment_name: str) -> PromptVersion | None:
        """Select a variant deterministically based on symbol hash."""
        versions = self.list_versions()
        if not versions:
            return None
        hash_val = int(hashlib.md5(f"{experiment_name}:{symbol}".encode()).hexdigest(), 16)
        idx = hash_val % len(versions)
        return versions[idx]

    def record_result(
        self,
        *,
        experiment_name: str,
        variant_name: str,
        interaction_id: int | None = None,
        order_action: str = "NONE",
        predicted_direction: str = "",
        actual_pnl: float = 0.0,
        was_profitable: bool | None = None,
    ) -> ExperimentResult:
        result = ExperimentResult(
            experiment_name=experiment_name,
            variant_name=variant_name,
            interaction_id=interaction_id,
            order_action=order_action,
            predicted_direction=predicted_direction,
            actual_pnl=actual_pnl,
            was_profitable=was_profitable,
        )
        self.db.add(result)
        self.db.commit()
        self.db.refresh(result)
        return result

    def get_experiment_summary(self, experiment_name: str) -> list[dict[str, Any]]:
        results = (
            self.db.query(ExperimentResult)
            .filter(ExperimentResult.experiment_name == experiment_name)
            .all()
        )
        by_variant: dict[str, list[ExperimentResult]] = {}
        for r in results:
            by_variant.setdefault(r.variant_name, []).append(r)

        summary = []
        for variant_name, items in by_variant.items():
            total = len(items)
            profitable = sum(1 for i in items if i.was_profitable)
            avg_pnl = sum(i.actual_pnl for i in items) / total if total > 0 else 0.0
            summary.append({
                "variant_name": variant_name,
                "total_count": total,
                "profitable_count": profitable,
                "avg_pnl": avg_pnl,
                "win_rate": profitable / total if total > 0 else 0.0,
            })
        return summary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_ab_test_manager.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/domain/experiment/ tests/test_ab_test_manager.py
git commit -m "feat(experiment): add ABTestManager for prompt versioning"
```

---

### Task 10: 实验管理 API

**Files:**
- Create: `backend/app/api/experiments.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Create experiments API router**

```python
# backend/app/api/experiments.py
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.domain.experiment.ab_test_manager import ABTestManager
from app.schemas import (
    ExperimentResultResponse,
    ExperimentSummary,
    MessageResponse,
    PromptVersionCreate,
    PromptVersionResponse,
)

router = APIRouter(prefix="/api/experiments", tags=["experiments"])
logger = logging.getLogger("auto_trade.experiments")


@router.get("/versions", response_model=List[PromptVersionResponse])
def list_versions(db: Session = Depends(get_db)) -> list[PromptVersionResponse]:
    manager = ABTestManager(db)
    return [PromptVersionResponse.model_validate(v) for v in manager.list_versions()]


@router.post("/versions", response_model=PromptVersionResponse)
def create_version(
    payload: PromptVersionCreate,
    db: Session = Depends(get_db),
) -> PromptVersionResponse:
    manager = ABTestManager(db)
    version = manager.create_version(
        name=payload.name,
        version=payload.version,
        description=payload.description,
        template=payload.template,
    )
    return PromptVersionResponse.model_validate(version)


@router.post("/versions/{version_id}/activate", response_model=MessageResponse)
def activate_version(
    version_id: int,
    db: Session = Depends(get_db),
) -> MessageResponse:
    manager = ABTestManager(db)
    try:
        manager.activate_version(version_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MessageResponse(message="activated")


@router.get("/versions/active", response_model=PromptVersionResponse | None)
def get_active_version(db: Session = Depends(get_db)) -> PromptVersionResponse | None:
    manager = ABTestManager(db)
    version = manager.get_active_version()
    if version is None:
        return None
    return PromptVersionResponse.model_validate(version)


@router.get("/{experiment_name}/summary")
def get_experiment_summary(
    experiment_name: str,
    db: Session = Depends(get_db),
) -> list[dict]:
    manager = ABTestManager(db)
    return manager.get_experiment_summary(experiment_name)
```

- [ ] **Step 2: Mount router in `main.py`**

Add to `backend/app/main.py` in the router mounting section:

```python
from app.api.experiments import router as experiments_router
app.include_router(experiments_router)
```

- [ ] **Step 3: Run tests**

Run: `cd backend && python -m pytest tests/ -v -k "experiment or ab_test" --timeout=30`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
cd backend && git add app/api/experiments.py app/main.py
git commit -m "feat(api): add experiment management API endpoints"
```

---

### Task 11: LLMAdvisorService 集成 PromptBuilder 和实验变体

**Files:**
- Modify: `backend/app/services/llm_advisor_service.py`
- Modify: `backend/tests/test_llm_advisor.py`

- [ ] **Step 1: Update `analyze` method to pass new indicator data**

In `llm_advisor_service.py`, update the `analyze` method to pass `rsi`, `macd`, `volume_analysis` from `market_data` to `build_prompt`:

```python
# In analyze(), update the build_prompt call:
prompt = self._data_aggregator.build_prompt(
    # ... existing params ...
    rsi=market_data.get("rsi", 0.0),
    macd=market_data.get("macd"),
    volume_analysis=market_data.get("volume_analysis"),
)
```

Do the same for the `preview` method.

- [ ] **Step 2: Add experiment variant support**

Add a method to optionally load a prompt template from an active experiment:

```python
def _get_active_prompt_template(self) -> str | None:
    """Load active prompt template from experiment if available."""
    try:
        from app.database import SessionLocal
        from app.domain.experiment.ab_test_manager import ABTestManager
        db = SessionLocal()
        try:
            manager = ABTestManager(db)
            active = manager.get_active_version()
            return active.template if active else None
        finally:
            db.close()
    except Exception:
        logger.debug("no active experiment variant available")
        return None
```

- [ ] **Step 3: Add test for new indicator passthrough**

Append to `backend/tests/test_llm_advisor.py`:

```python
def test_analyze_passes_new_indicators_to_prompt(monkeypatch):
    """Verify RSI, MACD, volume_analysis are included in prompt context."""
    from app.services.data_aggregator import DataAggregator

    captured_prompt = {}

    def mock_fetch(self, symbol, market):
        return {
            "daily_candles": [],
            "minute_candles": [],
            "current_price": 100.0,
            "atr": 3.0,
            "bb_upper": 110.0,
            "bb_middle": 100.0,
            "bb_lower": 90.0,
            "rsi": 65.0,
            "macd": {"macd": 1.5, "signal": 1.0, "histogram": 0.5},
            "volume_analysis": {"avg_volume": 50000.0, "volume_ratio": 1.2, "trend": "normal"},
        }

    original_build = DataAggregator.build_prompt

    def capturing_build(**kwargs):
        captured_prompt.update(kwargs)
        return original_build(**kwargs)

    monkeypatch.setattr(DataAggregator, "fetch_market_data", mock_fetch)
    monkeypatch.setattr(DataAggregator, "build_prompt", staticmethod(capturing_build))
    # ... rest of test setup with mock httpx ...
```

- [ ] **Step 4: Run all LLM advisor tests**

Run: `cd backend && python -m pytest tests/test_llm_advisor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/services/llm_advisor_service.py tests/test_llm_advisor.py
git commit -m "feat(llm): integrate PromptBuilder and new indicators into LLMAdvisorService"
```

---

### Task 12: Phase 1 综合验证

- [ ] **Step 1: Run full test suite**

Run: `cd backend && python -m pytest tests/ -v --timeout=60`
Expected: ≥ 503 tests passed (493 baseline + ~10 new)

- [ ] **Step 2: Run type check**

Run: `cd backend && python -m basedpyright`
Expected: 0 errors, 0 warnings

- [ ] **Step 3: Run frontend build**

Run: `cd frontend && npm run type-check && npm run build`
Expected: PASS

- [ ] **Step 4: Commit any final adjustments**

```bash
git add -A && git commit -m "chore: Phase 1 verification complete"
```

---

## Phase 2: 市场情绪 + 多时间框架 + 性能追踪

### Task 13: 市场情绪模块

**Files:**
- Create: `backend/app/domain/sentiment/__init__.py`
- Create: `backend/app/domain/sentiment/market_sentiment.py`
- Create: `backend/app/domain/prompt/sentiment_module.py`
- Test: `backend/tests/test_sentiment.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_sentiment.py
from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_sentiment.db",
)

import pytest
from app.domain.sentiment.market_sentiment import MarketSentimentAnalyzer
from app.domain.prompt.sentiment_module import SentimentModule


class TestMarketSentiment:
    def test_analyze_from_price_data_uptrend(self) -> None:
        analyzer = MarketSentimentAnalyzer()
        # Simulate uptrend: prices rising over 5 days
        price_changes = [1.0, 2.0, 1.5, 3.0, 2.5]
        result = analyzer.analyze_from_price_changes(price_changes)
        assert result["sentiment"] in ("bullish", "neutral")
        assert -1.0 <= result["score"] <= 1.0

    def test_analyze_from_price_data_downtrend(self) -> None:
        analyzer = MarketSentimentAnalyzer()
        price_changes = [-2.0, -1.5, -3.0, -1.0, -2.5]
        result = analyzer.analyze_from_price_changes(price_changes)
        assert result["sentiment"] in ("bearish", "neutral")
        assert result["score"] < 0

    def test_analyze_empty_returns_neutral(self) -> None:
        analyzer = MarketSentimentAnalyzer()
        result = analyzer.analyze_from_price_changes([])
        assert result["sentiment"] == "neutral"
        assert result["score"] == 0.0


class TestSentimentModule:
    def test_renders_bullish_sentiment(self) -> None:
        module = SentimentModule()
        context = {
            "sentiment": {"sentiment": "bullish", "score": 0.6, "description": "市场情绪偏多"},
        }
        result = module.render(context)
        assert "偏多" in result or "bullish" in result

    def test_renders_no_data_when_missing(self) -> None:
        module = SentimentModule()
        context = {}
        result = module.render(context)
        assert "无" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_sentiment.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement MarketSentimentAnalyzer**

```python
# backend/app/domain/sentiment/__init__.py
```

```python
# backend/app/domain/sentiment/market_sentiment.py
from __future__ import annotations

import statistics


class MarketSentimentAnalyzer:
    """Derives market sentiment from price action data."""

    def analyze_from_price_changes(self, price_changes: list[float]) -> dict:
        """Analyze sentiment from a series of price changes.

        Args:
            price_changes: List of price deltas (positive = up, negative = down).

        Returns:
            Dict with 'sentiment' (bullish/bearish/neutral), 'score' (-1 to 1), 'description'.
        """
        if not price_changes:
            return {"sentiment": "neutral", "score": 0.0, "description": "无价格数据"}

        avg_change = statistics.mean(price_changes)
        positive_count = sum(1 for c in price_changes if c > 0)
        negative_count = sum(1 for c in price_changes if c < 0)
        total = len(price_changes)

        # Score: normalized average + direction bias
        max_abs = max(abs(c) for c in price_changes) if price_changes else 1.0
        if max_abs == 0:
            normalized = 0.0
        else:
            normalized = avg_change / max_abs

        direction_bias = (positive_count - negative_count) / total
        score = (normalized * 0.6 + direction_bias * 0.4)
        score = max(-1.0, min(1.0, score))

        if score > 0.2:
            sentiment = "bullish"
            description = f"市场情绪偏多（得分 {score:.2f}）"
        elif score < -0.2:
            sentiment = "bearish"
            description = f"市场情绪偏空（得分 {score:.2f}）"
        else:
            sentiment = "neutral"
            description = f"市场情绪中性（得分 {score:.2f}）"

        return {"sentiment": sentiment, "score": score, "description": description}
```

- [ ] **Step 4: Implement SentimentModule for prompt**

```python
# backend/app/domain/prompt/sentiment_module.py
from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class SentimentModule(PromptModule):
    """Renders market sentiment data in the prompt."""

    def render(self, context: dict[str, Any]) -> str:
        sentiment = context.get("sentiment")
        if not sentiment:
            return "## 市场情绪\n无"

        description = sentiment.get("description", "无")
        score = sentiment.get("score", 0.0)
        label = sentiment.get("sentiment", "neutral")

        return f"""## 市场情绪
- 情绪倾向: {label}
- 情绪得分: {score:.2f}
- 描述: {description}"""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_sentiment.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
cd backend && git add app/domain/sentiment/ app/domain/prompt/sentiment_module.py tests/test_sentiment.py
git commit -m "feat(sentiment): add MarketSentimentAnalyzer and SentimentModule"
```

---

### Task 14: 集成情绪数据到 DataAggregator 和 PromptBuilder

**Files:**
- Modify: `backend/app/services/data_aggregator.py`
- Modify: `backend/app/domain/prompt/prompt_builder.py` (no change needed, just add module)

- [ ] **Step 1: Add sentiment to `fetch_market_data` return**

In `data_aggregator.py`, after computing volume_analysis, add:

```python
from app.domain.sentiment.market_sentiment import MarketSentimentAnalyzer

# In fetch_market_data, compute price changes for sentiment:
daily_closes = [c.close for c in daily_candles]
price_changes = [daily_closes[i] - daily_closes[i - 1] for i in range(1, len(daily_closes))]
sentiment_analyzer = MarketSentimentAnalyzer()
sentiment = sentiment_analyzer.analyze_from_price_changes(price_changes[-10:])

# Add to return dict:
"sentiment": sentiment,
```

- [ ] **Step 2: Add SentimentModule to PromptBuilder in `build_prompt`**

```python
from app.domain.prompt.sentiment_module import SentimentModule

# In build_prompt, add to context:
context["sentiment"] = sentiment or {"sentiment": "neutral", "score": 0.0, "description": "无"}

# Add module:
builder.add_module(SentimentModule())  # After ContextModule, before StrategyModule
```

- [ ] **Step 3: Update existing tests**

Update `test_data_aggregator.py` to verify `sentiment` is in the return dict.

- [ ] **Step 4: Run all tests**

Run: `cd backend && python -m pytest tests/test_data_aggregator.py tests/test_sentiment.py tests/test_prompt_builder.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/services/data_aggregator.py tests/test_data_aggregator.py
git commit -m "feat(data): integrate sentiment analysis into DataAggregator"
```

---

### Task 15: 多时间框架分析支持

**Files:**
- Modify: `backend/app/domain/analysis/technical_indicators.py`
- Modify: `backend/app/services/data_aggregator.py`

- [ ] **Step 1: Add multi-timeframe analysis method**

Add to `TechnicalIndicators`:

```python
@classmethod
def analyze_multi_timeframe(
    cls,
    daily_closes: list[float],
    minute_closes: list[float],
) -> dict[str, Any]:
    """Analyze trend alignment across timeframes."""
    daily_trend = "neutral"
    minute_trend = "neutral"

    if len(daily_closes) >= 5:
        daily_sma5 = statistics.mean(daily_closes[-5:])
        daily_current = daily_closes[-1]
        if daily_current > daily_sma5 * 1.01:
            daily_trend = "up"
        elif daily_current < daily_sma5 * 0.99:
            daily_trend = "down"

    if len(minute_closes) >= 20:
        minute_sma20 = statistics.mean(minute_closes[-20:])
        minute_current = minute_closes[-1]
        if minute_current > minute_sma20 * 1.005:
            minute_trend = "up"
        elif minute_current < minute_sma20 * 0.995:
            minute_trend = "down"

    aligned = daily_trend == minute_trend and daily_trend != "neutral"

    return {
        "daily_trend": daily_trend,
        "minute_trend": minute_trend,
        "aligned": aligned,
        "description": f"日线趋势: {daily_trend}, 分钟趋势: {minute_trend}" + (", 趋势一致" if aligned else ""),
    }
```

- [ ] **Step 2: Add tests for multi-timeframe**

Append to `backend/tests/test_technical_indicators.py`:

```python
class TestMultiTimeframe:
    def test_aligned_uptrend(self) -> None:
        daily = [100.0 + i for i in range(10)]
        minute = [105.0 + i * 0.1 for i in range(30)]
        result = TechnicalIndicators.analyze_multi_timeframe(daily, minute)
        assert result["aligned"] is True
        assert result["daily_trend"] == "up"

    def test_mixed_trends_not_aligned(self) -> None:
        daily = [100.0 + i for i in range(10)]  # up
        minute = [110.0 - i * 0.1 for i in range(30)]  # down
        result = TechnicalIndicators.analyze_multi_timeframe(daily, minute)
        assert result["aligned"] is False

    def test_short_data_returns_neutral(self) -> None:
        result = TechnicalIndicators.analyze_multi_timeframe([100.0], [100.0])
        assert result["daily_trend"] == "neutral"
        assert result["minute_trend"] == "neutral"
```

- [ ] **Step 3: Integrate into DataAggregator**

In `fetch_market_data`, add:

```python
daily_closes = [c.close for c in daily_candles]
minute_closes = [c.close for c in minute_candles]
multi_tf = TechnicalIndicators.analyze_multi_timeframe(daily_closes, minute_closes)

# Add to return dict:
"multi_timeframe": multi_tf,
```

- [ ] **Step 4: Run tests**

Run: `cd backend && python -m pytest tests/test_technical_indicators.py tests/test_data_aggregator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/domain/analysis/technical_indicators.py app/services/data_aggregator.py tests/test_technical_indicators.py
git commit -m "feat(analysis): add multi-timeframe trend alignment analysis"
```

---

### Task 16: 性能追踪器

**Files:**
- Create: `backend/app/domain/performance/__init__.py`
- Create: `backend/app/domain/performance/performance_tracker.py`
- Create: `backend/app/api/performance.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_performance_tracker.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_performance_tracker.py
from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_performance.db",
)

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, ExperimentResult
from app.domain.performance.performance_tracker import PerformanceTracker


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test_perf.db")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _seed_results(db: Session, experiment: str, variant: str, count: int, win_rate: float):
    import random
    random.seed(42)
    for i in range(count):
        profitable = random.random() < win_rate
        db.add(ExperimentResult(
            experiment_name=experiment,
            variant_name=variant,
            interaction_id=i + 1,
            order_action="BUY_NOW" if i % 2 == 0 else "SELL_NOW",
            predicted_direction="UP" if i % 2 == 0 else "DOWN",
            actual_pnl=50.0 if profitable else -30.0,
            was_profitable=profitable,
        ))
    db.commit()


class TestPerformanceTracker:
    def test_get_overall_stats(self, db_session: Session) -> None:
        _seed_results(db_session, "exp1", "v1", 20, 0.6)
        tracker = PerformanceTracker(db_session)
        stats = tracker.get_overall_stats("exp1")

        assert stats["total_trades"] == 20
        assert 0.4 < stats["win_rate"] < 0.8  # ~60% with seed
        assert stats["total_pnl"] != 0

    def test_get_variant_comparison(self, db_session: Session) -> None:
        _seed_results(db_session, "exp1", "v1", 20, 0.6)
        _seed_results(db_session, "exp1", "v2", 20, 0.4)
        tracker = PerformanceTracker(db_session)

        comparison = tracker.compare_variants("exp1")
        assert len(comparison) == 2
        v1 = next(c for c in comparison if c["variant"] == "v1")
        v2 = next(c for c in comparison if c["variant"] == "v2")
        assert v1["win_rate"] > v2["win_rate"]

    def test_get_recommendations(self, db_session: Session) -> None:
        _seed_results(db_session, "exp1", "v1", 30, 0.3)  # Poor performance
        tracker = PerformanceTracker(db_session)
        recs = tracker.get_recommendations("exp1")
        assert len(recs) > 0
        assert any("v1" in r for r in recs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_performance_tracker.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement PerformanceTracker**

```python
# backend/app/domain/performance/__init__.py
```

```python
# backend/app/domain/performance/performance_tracker.py
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import ExperimentResult


class PerformanceTracker:
    """Tracks and analyzes LLM prediction performance."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_overall_stats(self, experiment_name: str) -> dict[str, Any]:
        results = (
            self.db.query(ExperimentResult)
            .filter(ExperimentResult.experiment_name == experiment_name)
            .all()
        )
        if not results:
            return {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}

        total = len(results)
        profitable = sum(1 for r in results if r.was_profitable)
        total_pnl = sum(r.actual_pnl for r in results)

        return {
            "total_trades": total,
            "win_rate": profitable / total,
            "total_pnl": total_pnl,
            "avg_pnl": total_pnl / total,
        }

    def compare_variants(self, experiment_name: str) -> list[dict[str, Any]]:
        results = (
            self.db.query(ExperimentResult)
            .filter(ExperimentResult.experiment_name == experiment_name)
            .all()
        )
        by_variant: dict[str, list[ExperimentResult]] = {}
        for r in results:
            by_variant.setdefault(r.variant_name, []).append(r)

        comparison = []
        for variant, items in by_variant.items():
            total = len(items)
            profitable = sum(1 for i in items if i.was_profitable)
            total_pnl = sum(i.actual_pnl for i in items)
            comparison.append({
                "variant": variant,
                "total_trades": total,
                "win_rate": profitable / total if total > 0 else 0.0,
                "total_pnl": total_pnl,
                "avg_pnl": total_pnl / total if total > 0 else 0.0,
            })
        return comparison

    def get_recommendations(self, experiment_name: str) -> list[str]:
        comparison = self.compare_variants(experiment_name)
        recommendations = []

        for variant in comparison:
            if variant["total_trades"] < 10:
                recommendations.append(
                    f"变体 '{variant['variant']}' 样本不足（{variant['total_trades']} 笔），建议继续收集数据"
                )
            elif variant["win_rate"] < 0.4:
                recommendations.append(
                    f"变体 '{variant['variant']}' 胜率偏低（{variant['win_rate']:.1%}），建议优化 prompt 或停用"
                )
            elif variant["win_rate"] > 0.6:
                recommendations.append(
                    f"变体 '{variant['variant']}' 表现优秀（胜率 {variant['win_rate']:.1%}），建议设为主要版本"
                )

        if not recommendations:
            recommendations.append("所有变体表现正常，无需特别调整")

        return recommendations
```

- [ ] **Step 4: Create performance API**

```python
# backend/app/api/performance.py
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.domain.performance.performance_tracker import PerformanceTracker

router = APIRouter(prefix="/api/performance", tags=["performance"])
logger = logging.getLogger("auto_trade.performance")


@router.get("/stats")
def get_stats(
    experiment: str = Query(..., description="Experiment name"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tracker = PerformanceTracker(db)
    return tracker.get_overall_stats(experiment)


@router.get("/compare")
def compare_variants(
    experiment: str = Query(..., description="Experiment name"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tracker = PerformanceTracker(db)
    return tracker.compare_variants(experiment)


@router.get("/recommendations")
def get_recommendations(
    experiment: str = Query(..., description="Experiment name"),
    db: Session = Depends(get_db),
) -> list[str]:
    tracker = PerformanceTracker(db)
    return tracker.get_recommendations(experiment)
```

- [ ] **Step 5: Mount router in `main.py`**

```python
from app.api.performance import router as performance_router
app.include_router(performance_router)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_performance_tracker.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
cd backend && git add app/domain/performance/ app/api/performance.py app/main.py tests/test_performance_tracker.py
git commit -m "feat(performance): add PerformanceTracker and API endpoints"
```

---

### Task 17: Phase 2 综合验证

- [ ] **Step 1: Run full test suite**

Run: `cd backend && python -m pytest tests/ -v --timeout=60`
Expected: ≥ 515 tests passed (493 baseline + ~22 new)

- [ ] **Step 2: Run type check**

Run: `cd backend && python -m basedpyright`
Expected: 0 errors, 0 warnings

- [ ] **Step 3: Run frontend build**

Run: `cd frontend && npm run type-check && npm run build`
Expected: PASS

- [ ] **Step 4: Update Roadmap.md**

Mark the LLM Prompt Engineering Optimization iteration as complete.

- [ ] **Step 5: Final commit**

```bash
git add -A && git commit -m "feat: LLM prompt engineering optimization complete (Phase 1 + Phase 2)"
```

---

## Summary

| Task | Description | New Tests |
|------|-------------|-----------|
| 1 | PromptModule base + SystemModule | 2 |
| 2 | ContextModule (candles + indicators) | 3 |
| 3 | StrategyModule (position + trades) | 2 |
| 4 | OutputModule (JSON format + constraints) | 2 |
| 5 | PromptBuilder orchestrator | 4 |
| 6 | Technical indicators (RSI, MACD, volume) | 11 |
| 7 | DataAggregator integration | 1 |
| 8 | Database models (PromptVersion, ExperimentResult) | 0 |
| 9 | ABTestManager | 6 |
| 10 | Experiments API | 0 |
| 11 | LLMAdvisorService integration | 1 |
| 12 | Phase 1 verification | 0 |
| 13 | Market sentiment module | 5 |
| 14 | Sentiment integration | 0 |
| 15 | Multi-timeframe analysis | 3 |
| 16 | Performance tracker + API | 3 |
| 17 | Phase 2 verification | 0 |
| **Total** | | **~43 new tests** |
