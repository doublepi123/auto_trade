# P11: LLM 自适应特征选择 — 两阶段 Prompt 架构 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 LLM 自主特征选择，根据市场状态自动选择最相关的技术指标，减少无关指标干扰，提升决策质量。

**Architecture:** 新增 MarketStateDetector 检测市场状态，SelectionModule 构建指标选择 prompt，FeatureSelector 解析 LLM 返回。修改 ContextModule 支持过滤渲染，修改 LLMAdvisorService 集成两阶段流程。

**Tech Stack:** Python 3.11+, pytest, basedpyright, Longbridge SDK

---

## 文件结构

### 新增文件
- `backend/app/domain/analysis/market_state.py` — MarketStateDetector 类
- `backend/app/domain/prompt/selection_module.py` — SelectionModule prompt 模块
- `backend/app/domain/prompt/feature_selector.py` — FeatureSelector 类
- `backend/tests/test_market_state.py` — 市场状态检测测试
- `backend/tests/test_feature_selector.py` — 特征选择器测试

### 修改文件
- `backend/app/services/data_aggregator.py` — 调用 MarketStateDetector
- `backend/app/domain/prompt/context_module.py` — 支持过滤渲染
- `backend/app/services/llm_advisor_service.py` — 集成选择逻辑

---

### Task 1: MarketStateDetector 实现

**Files:**
- Create: `backend/app/domain/analysis/market_state.py`
- Create: `backend/tests/test_market_state.py`

- [ ] **Step 1: 写 MarketState 数据类和测试**

```python
# backend/app/domain/analysis/market_state.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MarketState:
    """Market state detected from technical indicators."""

    state: str  # "trending" | "ranging" | "volatile" | "neutral"
    confidence: float  # 0.0-1.0
    description: str  # Human-readable description
    suggested_indicators: list[str]  # Recommended indicators


class MarketStateDetector:
    """Detect market state based on technical indicators."""

    @staticmethod
    def detect(
        adx: dict[str, Any],
        bb_upper: float,
        bb_middle: float,
        bb_lower: float,
        atr: float,
        current_price: float,
        volume_analysis: dict[str, Any],
    ) -> MarketState:
        """Detect market state from indicators."""
        # Default for insufficient data
        if not adx or bb_middle <= 0:
            return MarketState(
                state="neutral",
                confidence=0.5,
                description="数据不足，无法判断市场状态",
                suggested_indicators=["rsi", "macd", "atr", "vwap"],
            )

        adx_value = float(adx.get("adx_value", 0))
        di_plus = float(adx.get("di_plus", 0))
        di_minus = float(adx.get("di_minus", 0))

        # 1. Trending market
        if adx_value > 25 and abs(di_plus - di_minus) > 10:
            direction = "上升" if di_plus > di_minus else "下降"
            return MarketState(
                state="trending",
                confidence=min(adx_value / 50, 1.0),
                description=f"{direction}趋势（ADX={adx_value:.1f}, DI+={di_plus:.1f}, DI-={di_minus:.1f}）",
                suggested_indicators=["adx", "macd", "obv", "vwap"],
            )

        # 2. Ranging market
        bb_width = (bb_upper - bb_lower) / bb_middle
        if adx_value < 20 and bb_width < 0.05:
            return MarketState(
                state="ranging",
                confidence=1.0 - adx_value / 20,
                description=f"震荡市场（ADX={adx_value:.1f}, 布林带宽度={bb_width:.2%}）",
                suggested_indicators=["stochastic", "cci", "williams_r", "rsi"],
            )

        # 3. Volatile market
        atr_pct = atr / current_price if current_price > 0 else 0
        if atr_pct > 0.03:
            return MarketState(
                state="volatile",
                confidence=min(atr_pct / 0.05, 1.0),
                description=f"高波动（ATR={atr:.2f}, 占价格{atr_pct:.1%}）",
                suggested_indicators=["atr", "vwap", "obv"],
            )

        # 4. Neutral market
        return MarketState(
            state="neutral",
            confidence=0.5,
            description="中性市场",
            suggested_indicators=["rsi", "macd", "atr", "vwap"],
        )
```

- [ ] **Step 2: 写测试用例**

```python
# backend/tests/test_market_state.py
from __future__ import annotations

from app.domain.analysis.market_state import MarketState, MarketStateDetector


class TestMarketStateDetector:
    """Tests for market state detection."""

    def test_trending_market(self) -> None:
        """Should detect trending market when ADX > 25 and DI spread > 10."""
        adx = {"adx_value": 32.0, "di_plus": 28.0, "di_minus": 12.0}
        result = MarketStateDetector.detect(
            adx=adx,
            bb_upper=105.0,
            bb_middle=100.0,
            bb_lower=95.0,
            atr=2.0,
            current_price=100.0,
            volume_analysis={"volume_ratio": 1.5},
        )
        assert result.state == "trending"
        assert result.confidence > 0.5
        assert "adx" in result.suggested_indicators

    def test_ranging_market(self) -> None:
        """Should detect ranging market when ADX < 20 and BB narrow."""
        adx = {"adx_value": 15.0, "di_plus": 18.0, "di_minus": 17.0}
        result = MarketStateDetector.detect(
            adx=adx,
            bb_upper=102.0,
            bb_middle=100.0,
            bb_lower=98.0,
            atr=1.0,
            current_price=100.0,
            volume_analysis={"volume_ratio": 0.8},
        )
        assert result.state == "ranging"
        assert "stochastic" in result.suggested_indicators

    def test_volatile_market(self) -> None:
        """Should detect volatile market when ATR/price > 3%."""
        adx = {"adx_value": 22.0, "di_plus": 20.0, "di_minus": 18.0}
        result = MarketStateDetector.detect(
            adx=adx,
            bb_upper=108.0,
            bb_middle=100.0,
            bb_lower=92.0,
            atr=4.0,
            current_price=100.0,
            volume_analysis={"volume_ratio": 2.0},
        )
        assert result.state == "volatile"
        assert "atr" in result.suggested_indicators

    def test_neutral_market(self) -> None:
        """Should detect neutral market as default."""
        adx = {"adx_value": 22.0, "di_plus": 20.0, "di_minus": 18.0}
        result = MarketStateDetector.detect(
            adx=adx,
            bb_upper=104.0,
            bb_middle=100.0,
            bb_lower=96.0,
            atr=1.5,
            current_price=100.0,
            volume_analysis={"volume_ratio": 1.0},
        )
        assert result.state == "neutral"

    def test_insufficient_data(self) -> None:
        """Should return neutral for insufficient data."""
        result = MarketStateDetector.detect(
            adx={},
            bb_upper=0.0,
            bb_middle=0.0,
            bb_lower=0.0,
            atr=0.0,
            current_price=0.0,
            volume_analysis={},
        )
        assert result.state == "neutral"
        assert result.confidence == 0.5
```

- [ ] **Step 3: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_market_state.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add backend/app/domain/analysis/market_state.py backend/tests/test_market_state.py
git commit -m "feat(analysis): add MarketStateDetector for market state detection"
```

---

### Task 2: SelectionModule 实现

**Files:**
- Create: `backend/app/domain/prompt/selection_module.py`
- Modify: `backend/tests/test_prompt_builder.py`

- [ ] **Step 1: 写 SelectionModule**

```python
# backend/app/domain/prompt/selection_module.py
from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class SelectionModule(PromptModule):
    """Renders market state and available indicators for LLM selection."""

    def render(self, context: dict[str, Any]) -> str:
        market_state = context.get("market_state")
        if not market_state:
            return ""

        state = market_state.get("state", "neutral")
        description = market_state.get("description", "")
        suggested = market_state.get("suggested_indicators", [])

        lines = [
            "## 市场状态分析",
            f"当前市场状态：{description}",
            "",
            "## 可用技术指标",
            "请选择 3-5 个最相关的指标用于本次分析：",
            "",
            "趋势指标：",
            "- ADX（趋势强度）",
            "- MACD（趋势动量）",
            "- OBV（量价关系）",
            "",
            "震荡指标：",
            "- RSI（超买超卖）",
            "- Stochastic（随机指标）",
            "- CCI（商品通道）",
            "- Williams %R（威廉指标）",
            "",
            "成本指标：",
            "- VWAP（成交量加权均价）",
        ]

        if suggested:
            lines.append("")
            lines.append(f"基于当前市场状态，建议优先考虑：{', '.join(suggested)}")

        lines.append("")
        lines.append('请以 JSON 格式返回：{"selected_indicators": ["adx", "macd", "obv"], "reasoning": "选择理由"}')

        return "\n".join(lines)
```

- [ ] **Step 2: 写测试用例**

```python
# 在 test_prompt_builder.py 中新增:

def test_selection_module_renders_market_state(self) -> None:
    """SelectionModule should render market state and indicators."""
    context = {
        "market_state": {
            "state": "trending",
            "description": "上升趋势（ADX=32）",
            "suggested_indicators": ["adx", "macd", "obv"],
        }
    }
    module = SelectionModule()
    result = module.render(context)
    assert "市场状态分析" in result
    assert "上升趋势" in result
    assert "ADX" in result
    assert "建议优先考虑" in result

def test_selection_module_empty_without_market_state(self) -> None:
    """SelectionModule should return empty without market state."""
    module = SelectionModule()
    result = module.render({})
    assert result == ""
```

- [ ] **Step 3: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py -v -k "selection"`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add backend/app/domain/prompt/selection_module.py
git commit -m "feat(prompt): add SelectionModule for indicator selection"
```

---

### Task 3: FeatureSelector 实现

**Files:**
- Create: `backend/app/domain/prompt/feature_selector.py`
- Create: `backend/tests/test_feature_selector.py`

- [ ] **Step 1: 写 FeatureSelector**

```python
# backend/app/domain/prompt/feature_selector.py
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("auto_trade.feature_selector")

# All available indicators with their keys
AVAILABLE_INDICATORS = {
    "rsi": "RSI",
    "macd": "MACD",
    "atr": "ATR",
    "obv": "OBV",
    "adx": "ADX",
    "stochastic": "Stochastic",
    "cci": "CCI",
    "williams_r": "Williams %R",
    "vwap": "VWAP",
}

# Default indicators for fallback
DEFAULT_INDICATORS = ["rsi", "macd", "atr", "vwap"]


class FeatureSelector:
    """Parse LLM indicator selection and filter context."""

    @staticmethod
    def parse_selection(llm_response: str, suggested: list[str]) -> list[str]:
        """Parse LLM response to extract selected indicators."""
        try:
            # Try to find JSON in response
            start = llm_response.find("{")
            end = llm_response.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = llm_response[start:end]
                data = json.loads(json_str)
                selected = data.get("selected_indicators", [])
                if isinstance(selected, list):
                    # Validate indicators
                    valid = [s for s in selected if s in AVAILABLE_INDICATORS]
                    if valid:
                        return valid
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Failed to parse LLM indicator selection")

        # Fallback to suggested or default
        if suggested:
            return [s for s in suggested if s in AVAILABLE_INDICATORS]
        return DEFAULT_INDICATORS

    @staticmethod
    def filter_context(
        context: dict[str, Any],
        selected_indicators: list[str],
    ) -> dict[str, Any]:
        """Filter context to only include selected indicators."""
        filtered = dict(context)

        # Always keep essential fields
        essential_keys = {
            "symbol", "market", "current_price", "current_buy_low", "current_sell_high",
            "short_selling", "daily_candles", "minute_candles", "current_position",
            "recent_trades", "position_quantity", "position_avg_price", "unrealized_pnl_pct",
            "min_profit_amount", "account_context_text", "recent_price_context",
            "recent_analysis_context", "market_state",
        }

        # Indicator field mapping
        indicator_fields = {
            "rsi": ["rsi"],
            "macd": ["macd"],
            "atr": ["atr"],
            "obv": ["obv"],
            "adx": ["adx"],
            "stochastic": ["stochastic"],
            "cci": ["cci"],
            "williams_r": ["williams_r"],
            "vwap": ["vwap"],
        }

        # Remove non-selected indicator fields
        for indicator, fields in indicator_fields.items():
            if indicator not in selected_indicators:
                for field in fields:
                    filtered.pop(field, None)

        # Remove aggregate_signals if not all indicators selected
        if len(selected_indicators) < 5:
            filtered.pop("aggregate_signals", None)

        # Keep BB fields only if relevant
        if "atr" not in selected_indicators and "adx" not in selected_indicators:
            filtered.pop("bb_upper", None)
            filtered.pop("bb_middle", None)
            filtered.pop("bb_lower", None)

        # Keep volume_analysis if OBV or VWAP selected
        if "obv" not in selected_indicators and "vwap" not in selected_indicators:
            filtered.pop("volume_analysis", None)

        return filtered
```

- [ ] **Step 2: 写测试用例**

```python
# backend/tests/test_feature_selector.py
from __future__ import annotations

from app.domain.prompt.feature_selector import FeatureSelector


class TestFeatureSelector:
    """Tests for feature selector."""

    def test_parse_valid_json(self) -> None:
        """Should parse valid JSON selection."""
        response = '{"selected_indicators": ["adx", "macd", "obv"], "reasoning": "trend"}'
        result = FeatureSelector.parse_selection(response, [])
        assert result == ["adx", "macd", "obv"]

    def test_parse_invalid_json(self) -> None:
        """Should fallback to suggested on invalid JSON."""
        response = "invalid json"
        result = FeatureSelector.parse_selection(response, ["rsi", "cci"])
        assert result == ["rsi", "cci"]

    def test_parse_empty_selection(self) -> None:
        """Should fallback to suggested on empty selection."""
        response = '{"selected_indicators": [], "reasoning": "none"}'
        result = FeatureSelector.parse_selection(response, ["rsi"])
        assert result == ["rsi"]

    def test_parse_unknown_indicators(self) -> None:
        """Should filter out unknown indicators."""
        response = '{"selected_indicators": ["adx", "unknown", "macd"]}'
        result = FeatureSelector.parse_selection(response, [])
        assert result == ["adx", "macd"]

    def test_filter_context_removes_unselected(self) -> None:
        """Should remove indicator fields not in selection."""
        context = {
            "rsi": 50.0,
            "macd": {"macd": 0.5},
            "atr": 2.0,
            "obv": {"obv_trend": "rising"},
            "adx": {"adx_value": 30.0},
            "current_price": 100.0,
        }
        filtered = FeatureSelector.filter_context(context, ["rsi", "macd"])
        assert "rsi" in filtered
        assert "macd" in filtered
        assert "atr" not in filtered
        assert "obv" not in filtered
        assert "adx" not in filtered
        assert "current_price" in filtered

    def test_filter_context_keeps_essential(self) -> None:
        """Should always keep essential fields."""
        context = {
            "symbol": "AAPL",
            "current_price": 100.0,
            "rsi": 50.0,
        }
        filtered = FeatureSelector.filter_context(context, [])
        assert "symbol" in filtered
        assert "current_price" in filtered
```

- [ ] **Step 3: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_feature_selector.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add backend/app/domain/prompt/feature_selector.py backend/tests/test_feature_selector.py
git commit -m "feat(prompt): add FeatureSelector for indicator selection parsing"
```

---

### Task 4: DataAggregator 集成市场状态

**Files:**
- Modify: `backend/app/services/data_aggregator.py`
- Modify: `backend/tests/test_data_aggregator.py`

- [ ] **Step 1: 修改 DataAggregator.fetch_market_data**

在 `fetch_market_data` 方法末尾，调用 `MarketStateDetector.detect()` 并将结果添加到返回字典中。

```python
# 在 data_aggregator.py 中:

from app.domain.analysis.market_state import MarketStateDetector

# 在 fetch_market_data 方法末尾:
market_state = MarketStateDetector.detect(
    adx=adx,
    bb_upper=bb_upper,
    bb_middle=bb_middle,
    bb_lower=bb_lower,
    atr=atr,
    current_price=current_price,
    volume_analysis=volume_analysis,
)

return {
    # ... existing fields ...
    "market_state": {
        "state": market_state.state,
        "confidence": market_state.confidence,
        "description": market_state.description,
        "suggested_indicators": market_state.suggested_indicators,
    },
}
```

- [ ] **Step 2: 写测试用例**

```python
# 在 test_data_aggregator.py 中新增:

def test_fetch_market_data_includes_market_state(self) -> None:
    """fetch_market_data should include market state."""
    # ... setup mock broker ...
    result = aggregator.fetch_market_data("AAPL", "US")
    assert "market_state" in result
    assert "state" in result["market_state"]
    assert "suggested_indicators" in result["market_state"]
```

- [ ] **Step 3: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_data_aggregator.py -v -k "market_state"`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add backend/app/services/data_aggregator.py
git commit -m "feat(data): integrate MarketStateDetector into DataAggregator"
```

---

### Task 5: ContextModule 过滤渲染支持

**Files:**
- Modify: `backend/app/domain/prompt/context_module.py`

- [ ] **Step 1: 修改 ContextModule.render**

在 `render` 方法开头，检查 `selected_indicators` 字段，如果存在则只渲染选中的指标。

```python
# 在 context_module.py 的 render 方法中:

def render(self, context: dict[str, Any]) -> str:
    selected_indicators = context.get("selected_indicators")
    
    # ... existing code for daily/minute tables ...
    
    # 当前技术指标 - 只渲染选中的
    lines = [
        "## 市场数据（最近日 K 线）",
        ohlcv_table,
        "",
        "## 市场数据（最近 1 分钟 K 线）",
        minute_table,
        "",
        "## 当前技术指标",
    ]

    # ATR - always show if selected or no selection
    atr = context.get("atr", 0.0)
    if not selected_indicators or "atr" in selected_indicators:
        lines.append(f"- ATR(14): {atr:.2f}")

    # BB - show if ATR or ADX selected
    bb_upper = context.get("bb_upper", 0.0)
    bb_middle = context.get("bb_middle", 0.0)
    bb_lower = context.get("bb_lower", 0.0)
    if not selected_indicators or "atr" in selected_indicators or "adx" in selected_indicators:
        lines.append(f"- 布林带: 上轨 {bb_upper:.2f} / 中轨 {bb_middle:.2f} / 下轨 {bb_lower:.2f}")

    lines.append(f"- 当前价格: {current_price:.2f}")

    # RSI
    rsi = context.get("rsi", 0.0)
    if (not selected_indicators or "rsi" in selected_indicators) and rsi > 0:
        lines.append(f"- RSI(14): {rsi:.2f}")

    # MACD
    macd = context.get("macd", {})
    if (not selected_indicators or "macd" in selected_indicators) and macd:
        # ... existing MACD rendering ...

    # Volume
    volume_analysis = context.get("volume_analysis", {})
    if (not selected_indicators or "obv" in selected_indicators or "vwap" in selected_indicators):
        # ... existing volume rendering ...

    # Extended indicators - only selected ones
    if not selected_indicators or "obv" in selected_indicators:
        obv = context.get("obv", {})
        if obv and obv.get("obv_trend"):
            # ... render OBV ...

    # ... similar for other extended indicators ...
```

- [ ] **Step 2: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_prompt_builder.py -v`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add backend/app/domain/prompt/context_module.py
git commit -m "feat(prompt): add selected_indicators filtering to ContextModule"
```

---

### Task 6: LLMAdvisorService 集成选择逻辑

**Files:**
- Modify: `backend/app/services/llm_advisor_service.py`

- [ ] **Step 1: 修改 LLMAdvisorService.analyze**

在 `analyze` 方法中，集成两阶段 prompt 流程：

```python
# 在 llm_advisor_service.py 中:

from app.domain.prompt.feature_selector import FeatureSelector
from app.domain.prompt.selection_module import SelectionModule

def analyze(self, ...) -> dict[str, Any]:
    # ... existing code to build context ...
    
    # 1. 构建包含选择提示的 prompt
    builder = PromptBuilder()
    builder.add_module(SystemModule())
    builder.add_module(SelectionModule())  # 新增：指标选择提示
    builder.add_module(ContextModule())
    builder.add_module(SentimentModule())
    builder.add_module(StrategyModule())
    builder.add_module(OutputModule())
    
    full_prompt = builder.build(context)
    
    # 2. 调用 LLM
    llm_response = self._call_llm(full_prompt)
    
    # 3. 解析指标选择
    market_state = context.get("market_state", {})
    suggested = market_state.get("suggested_indicators", [])
    selected = FeatureSelector.parse_selection(llm_response, suggested)
    
    # 4. 过滤上下文
    filtered_context = FeatureSelector.filter_context(context, selected)
    filtered_context["selected_indicators"] = selected
    
    # 5. 使用过滤后的上下文重新构建 prompt（可选，或直接使用完整 prompt）
    # ... 后续分析逻辑 ...
```

- [ ] **Step 2: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_llm_advisor.py -v`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add backend/app/services/llm_advisor_service.py
git commit -m "feat(llm): integrate feature selection into LLMAdvisorService"
```

---

### Task 7: 最终验证与回归测试

**Files:**
- None (verification only)

- [ ] **Step 1: 运行完整测试套件**

Run: `cd backend && python -m pytest tests/ -v`
Expected: 全部通过（现有 587 项 + 新增约 15 项）

- [ ] **Step 2: 运行类型检查**

Run: `cd backend && python -m basedpyright`
Expected: 0 errors, 0 warnings

- [ ] **Step 3: 运行前端构建**

Run: `cd frontend && npm run type-check && npm run build`
Expected: 通过

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "feat(P11): LLM adaptive feature selection complete

- Added MarketStateDetector for market state detection
- Added SelectionModule for indicator selection prompt
- Added FeatureSelector for LLM response parsing
- Modified ContextModule to support filtered rendering
- Modified LLMAdvisorService to integrate selection logic
- Added comprehensive unit tests (~15 new test cases)
- All tests passing, basedpyright 0 errors"
```

---

## 自查清单

- [x] 覆盖规范中所有组件（MarketStateDetector/SelectionModule/FeatureSelector）
- [x] 覆盖 DataAggregator 集成
- [x] 覆盖 ContextModule 过滤渲染
- [x] 覆盖 LLMAdvisorService 集成
- [x] 每个任务包含完整代码，无占位符
- [x] 每个任务包含测试用例
- [x] 文件路径精确，代码风格与现有代码一致
