# P11: LLM 自适应特征选择 — 两阶段 Prompt 架构

**日期**: 2026-05-29
**状态**: 设计完成，待实现
**基线**: pytest 587 passed, basedpyright 0 errors

---

## 1. 背景与目标

### 1.1 背景

P10 已完成 6 个新技术指标（OBV/ADX/Stochastic/CCI/Williams %R/VWAP）+ aggregate_signals() 综合信号。当前 LLM 分析使用固定指标组合，无论市场状态如何都渲染所有指标。

**问题**：
- 趋势市场中，震荡指标（Stochastic/CCI/Williams %R）可能产生误导信号
- 震荡市场中，趋势指标（ADX/MACD）可能失效
- 所有指标同时渲染增加 prompt 长度和 LLM 处理负担

### 1.2 目标

实现 LLM 自主特征选择：
- 根据市场状态（趋势/震荡/高波动）自动选择最相关的指标组合
- 减少无关指标的干扰，提升 LLM 决策质量
- 通过回测验证"自适应选择"是否优于"固定指标"

### 1.3 约束

- **API 调用**：合并为一次调用，避免显著增加延迟和成本
- **架构**：扩展现有 PromptBuilder 和 ContextModule，保持向后兼容
- **验证**：全面验证（测试 + 回测 + 性能基准）

---

## 2. 架构设计

### 2.1 核心思路

在现有单阶段 prompt 基础上，新增市场状态检测 + 两阶段 prompt 架构，让 LLM 基于市场状态自主选择最相关的技术指标。

### 2.2 新增组件

| 组件 | 职责 | 位置 |
|------|------|------|
| `MarketStateDetector` | 基于基础指标判断市场状态 | `backend/app/domain/analysis/market_state.py` |
| `FeatureSelector` | 构建两阶段 prompt，解析 LLM 选择 | `backend/app/domain/prompt/feature_selector.py` |
| `SelectionModule` | 第一阶段 prompt 模块 | `backend/app/domain/prompt/selection_module.py` |

### 2.3 数据流

```
DataAggregator.fetch_market_data()
    ↓
MarketStateDetector.detect() → 市场状态（trending/ranging/volatile）
    ↓
SelectionModule.render() → 第一阶段 prompt（市场状态 + 可用指标）
    ↓
LLM 返回 {selected_indicators: [...], reasoning: "..."}
    ↓
ContextModule.render() → 第二阶段 prompt（只渲染选中指标）
    ↓
LLM 最终分析
```

### 2.4 文件变更

**新增文件**：
- `backend/app/domain/analysis/market_state.py` — MarketStateDetector 类
- `backend/app/domain/prompt/selection_module.py` — SelectionStage prompt 模块
- `backend/app/domain/prompt/feature_selector.py` — FeatureSelector 类
- `backend/tests/test_market_state.py` — 市场状态检测测试
- `backend/tests/test_feature_selector.py` — 特征选择器测试

**修改文件**：

| 文件 | 变更内容 |
|------|----------|
| `backend/app/services/data_aggregator.py` | 调用 MarketStateDetector，返回市场状态 |
| `backend/app/domain/prompt/prompt_builder.py` | 支持两阶段 prompt 构建 |
| `backend/app/domain/prompt/context_module.py` | 根据 LLM 选择过滤指标渲染 |
| `backend/app/services/llm_advisor_service.py` | 解析 LLM 返回的指标选择 |
| `backend/tests/test_data_aggregator.py` | 补充市场状态集成测试 |
| `backend/tests/test_prompt_builder.py` | 补充两阶段 prompt 测试 |

**不涉及**：
- 前端（指标选择是 LLM 内部行为，无需 UI 变更）
- 数据库（选择结果是临时的，不持久化）
- BrokerGateway（仅使用现有指标数据）

---

## 3. 市场状态检测

### 3.1 MarketStateDetector 逻辑

基于已计算的指标判断市场状态：

| 状态 | 判断条件 | 推荐指标 |
|------|----------|----------|
| **Trending** | ADX > 25 且 DI+/DI- 明确 | ADX, MACD, OBV, VWAP |
| **Ranging** | ADX < 20 且布林带收窄 | Stochastic, CCI, Williams %R, RSI |
| **Volatile** | ATR/价格 > 阈值 或 布林带扩张 | ATR, VWAP, OBV |
| **Breakout** | 价格突破布林带 + 成交量放大 | OBV, ADX, Volume |

### 3.2 输出格式

```python
@dataclass
class MarketState:
    state: str  # "trending" | "ranging" | "volatile" | "breakout"
    confidence: float  # 0.0-1.0
    description: str  # 人类可读描述
    suggested_indicators: list[str]  # 推荐指标列表
```

### 3.3 实现细节

```python
class MarketStateDetector:
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
        """Detect market state based on technical indicators."""
        # 1. 判断趋势状态
        adx_value = adx.get("adx_value", 0)
        di_plus = adx.get("di_plus", 0)
        di_minus = adx.get("di_minus", 0)
        
        if adx_value > 25 and abs(di_plus - di_minus) > 10:
            return MarketState(
                state="trending",
                confidence=min(adx_value / 50, 1.0),
                description=f"{'上升' if di_plus > di_minus else '下降'}趋势（ADX={adx_value:.1f}）",
                suggested_indicators=["adx", "macd", "obv", "vwap"],
            )
        
        # 2. 判断震荡状态
        bb_width = (bb_upper - bb_lower) / bb_middle if bb_middle > 0 else 0
        if adx_value < 20 and bb_width < 0.05:
            return MarketState(
                state="ranging",
                confidence=1.0 - adx_value / 20,
                description=f"震荡市场（ADX={adx_value:.1f}, 布林带宽度={bb_width:.2%}）",
                suggested_indicators=["stochastic", "cci", "williams_r", "rsi"],
            )
        
        # 3. 判断波动状态
        atr_pct = atr / current_price if current_price > 0 else 0
        if atr_pct > 0.03:
            return MarketState(
                state="volatile",
                confidence=min(atr_pct / 0.05, 1.0),
                description=f"高波动（ATR={atr:.2f}, 占价格{atr_pct:.1%}）",
                suggested_indicators=["atr", "vwap", "obv"],
            )
        
        # 4. 默认状态
        return MarketState(
            state="neutral",
            confidence=0.5,
            description="中性市场",
            suggested_indicators=["rsi", "macd", "atr", "vwap"],
        )
```

---

## 4. 两阶段 Prompt 设计

### 4.1 第一阶段 Prompt（指标选择）

```
## 市场状态分析
当前市场状态：强上升趋势（ADX=32, DI+=28, DI-=12）
波动率：中等（ATR=2.5, 占价格1.8%）
成交量：放量（量比=1.8）

## 可用技术指标
请选择 3-5 个最相关的指标用于本次分析：

趋势指标：
- ADX（趋势强度）
- MACD（趋势动量）
- OBV（量价关系）

震荡指标：
- RSI（超买超卖）
- Stochastic（随机指标）
- CCI（商品通道）
- Williams %R（威廉指标）

成本指标：
- VWAP（成交量加权均价）

请以 JSON 格式返回：
{
  "selected_indicators": ["adx", "macd", "obv"],
  "reasoning": "当前为趋势市场，选择趋势跟踪指标"
}
```

### 4.2 第二阶段 Prompt（最终分析）

基于 LLM 选择的指标，只渲染选中指标的详细数据，其余指标不渲染。

### 4.3 API 调用优化

**方案 B（推荐）**：合并为一次调用，在 prompt 中要求先选择再分析。

**Prompt 结构**：
```
[系统指令]
[市场状态 + 可用指标列表]
请先选择 3-5 个最相关的指标，然后基于选中指标进行分析。

[所有指标数据]
[策略状态]
[输出格式要求]
```

**优势**：
- 一次 API 调用，延迟和成本可控
- LLM 可以在分析过程中参考选择理由

---

## 5. 测试与验证策略

### 5.1 单元测试

**文件**: `backend/tests/test_market_state.py`

**测试用例**：
1. 趋势市场检测（ADX > 25, DI+/DI- 明确）
2. 震荡市场检测（ADX < 20, 布林带收窄）
3. 高波动市场检测（ATR/价格 > 阈值）
4. 边界条件：数据不足时返回默认状态
5. 异常数据：空指标返回中性状态

### 5.2 集成测试

**文件**: `backend/tests/test_feature_selector.py`

**测试用例**：
1. LLM 返回有效选择 → 正确解析
2. LLM 返回无效选择 → 回退到默认指标
3. LLM 返回空选择 → 回退到默认指标
4. 两阶段 prompt 完整流程验证

### 5.3 回测验证

**方法**：
- 使用现有 BacktestEngine 对比"固定指标" vs "LLM 自选指标"
- 基线：当前固定指标组合
- 实验组：LLM 根据市场状态自选指标

**指标**：
- 胜率
- 盈亏比
- 最大回撤
- 夏普比率

### 5.4 性能基准

**目标**：
- 两阶段 prompt 总延迟 < 2 倍单阶段延迟
- API 成本增幅 < 50%（方案 B 合并调用）

### 5.5 验收标准

- [ ] `pytest` 全通过（现有 587 项 + 新增约 20 项）
- [ ] `basedpyright` 0 errors
- [ ] `npm run type-check` + `npm run build` 通过
- [ ] 回测对比显示 LLM 自选指标有正向贡献（或至少不降低）

---

## 6. 实现计划

### 6.1 任务分解

| 任务 | 描述 | 预估工时 |
|------|------|----------|
| T1 | 实现 MarketStateDetector + 测试 | 0.5 天 |
| T2 | 实现 SelectionModule prompt 模块 | 0.5 天 |
| T3 | 实现 FeatureSelector（解析 LLM 返回） | 0.5 天 |
| T4 | 修改 DataAggregator 集成市场状态 | 0.5 天 |
| T5 | 修改 PromptBuilder 支持两阶段 | 0.5 天 |
| T6 | 修改 ContextModule 支持过滤渲染 | 0.5 天 |
| T7 | 修改 LLMAdvisorService 集成选择逻辑 | 0.5 天 |
| T8 | 回测验证 + 性能基准 | 0.5 天 |
| **总计** | | **4 天** |

### 6.2 依赖关系

- T1 可独立实现
- T2 依赖 T1（需要市场状态数据）
- T3 依赖 T2（需要 prompt 模块）
- T4-T7 依赖 T1-T3
- T8 依赖 T4-T7

---

## 7. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| LLM 选择不稳定 | 不同调用选择不同指标 | 提供 suggested_indicators 作为参考 |
| LLM 选择无效指标 | 回退到默认指标 | 严格验证 + 回退逻辑 |
| API 成本增加 | 运营成本上升 | 合并调用 + 控制增幅 < 50% |
| 市场状态误判 | 选择错误指标组合 | 多指标交叉验证 + 置信度阈值 |

---

## 8. 参考资料

- [Prompt Engineering Guide](https://www.promptingguide.ai/)
- [LLM Agent Architecture Patterns](https://lilianweng.github.io/posts/2023-06-23-agent/)
