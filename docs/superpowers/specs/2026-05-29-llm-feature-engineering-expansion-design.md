# P10: LLM 特征工程扩展 — 技术指标深度优化

**日期**: 2026-05-29
**状态**: 设计完成，待实现
**基线**: pytest 549 passed, basedpyright 0 errors

---

## 1. 背景与目标

### 1.1 背景

P9 已完成模块化 Prompt 架构 + RSI/MACD/Volume + A/B 测试框架 + 市场情绪 + 多时间框架分析。当前 LLM 决策基于以下特征：

- RSI(14)
- MACD(12,26,9)
- ATR(14)
- 布林带(20,2)
- 成交量分析（均量/量比/趋势）
- 市场情绪（基于价格变化）
- 多时间框架趋势对齐

### 1.2 目标

扩展经典技术指标覆盖，为 LLM 提供更全面的市场分析维度：

- **量价分析**：OBV（能量潮）— 检测量价背离
- **趋势强度**：ADX（平均趋向指数）— 判断趋势是否值得跟踪
- **超买超卖**：Stochastic、CCI、Williams %R — 捕捉反转点
- **机构成本**：VWAP（成交量加权平均价）— 机构成本参考

### 1.3 约束

- **数据源**：纯 Longbridge SDK（仅使用 `get_candlesticks` 返回的 K 线数据）
- **架构**：扩展现有 `TechnicalIndicators` 类，保持一致的代码风格
- **验证**：全面验证（测试 + 回测 + 性能基准）

---

## 2. 架构设计

### 2.1 核心思路

扩展现有 `TechnicalIndicators` 类，新增 6 个计算方法 + 1 个综合信号方法，保持与现有 RSI/MACD/ATR/BB 一致的架构模式。

### 2.2 新增指标清单

| 指标 | 全称 | 用途 | 最小数据需求 | 输出 |
|------|------|------|--------------|------|
| OBV | On-Balance Volume | 量价背离检测 | 收盘价 + 成交量序列 | OBV 序列 + 趋势 |
| ADX | Average Directional Index | 趋势强度 | 高低价 + 收盘价（14根） | ADX 值（0-100）+ 强度判断 |
| Stochastic | Stochastic Oscillator | 超买超卖 | 高低价 + 收盘价（14根） | %K、%D、信号 |
| CCI | Commodity Channel Index | 价格偏离度 | 高低价 + 收盘价（20根） | CCI 值 + 信号 |
| Williams %R | Williams Percent Range | 超买超卖 | 高低价 + 收盘价（14根） | %R 值 + 信号 |
| VWAP | Volume Weighted Average Price | 机构成本线 | 高低价 + 收盘价 + 成交量 | VWAP 值 + 相对位置 |

### 2.3 数据流

```
BrokerGateway.get_candlesticks()
    ↓
DataAggregator.fetch_market_data()
    ↓
TechnicalIndicators.calculate_*()  ← 新增6个方法
    ↓
ContextModule.render()  ← 扩展prompt渲染
    ↓
LLM 分析
```

### 2.4 文件变更

**新增文件**：
- `backend/tests/test_technical_indicators.py` — 扩展指标单元测试

**修改文件**：

| 文件 | 变更内容 |
|------|----------|
| `backend/app/domain/analysis/technical_indicators.py` | 新增 6 个计算方法 + `aggregate_signals()` |
| `backend/app/services/data_aggregator.py` | 调用新指标，扩展返回字段 |
| `backend/app/domain/prompt/context_module.py` | 渲染新指标摘要 |
| `backend/tests/test_data_aggregator.py` | 补充新指标集成测试 |
| `backend/tests/test_llm_advisor.py` | 验证 prompt 包含新指标 |

**不涉及**：
- 前端（指标数据通过现有 prompt 传递给 LLM，无需 UI 变更）
- 数据库（指标是实时计算，不持久化）
- BrokerGateway（仅使用现有 K 线数据）

---

## 3. 指标实现细节

### 3.1 OBV（能量潮）

**计算逻辑**：
```
OBV[0] = 0
for i in range(1, len(closes)):
    if closes[i] > closes[i-1]:
        OBV[i] = OBV[i-1] + volumes[i]
    elif closes[i] < closes[i-1]:
        OBV[i] = OBV[i-1] - volumes[i]
    else:
        OBV[i] = OBV[i-1]
```

**输出**：
- `obv_values`: OBV 序列
- `obv_trend`: "rising" / "falling" / "flat"（基于最近 5 日 OBV 斜率）
- `price_obv_divergence`: "bullish" / "bearish" / "none"（价格与 OBV 趋势是否背离）

**用途**：检测量价背离（价格上涨但 OBV 下降 → 看跌信号）

### 3.2 ADX（平均趋向指数）

**计算逻辑**：
1. 计算 +DM / -DM（方向运动）
2. 计算 True Range
3. 计算 DI+ / DI-（方向指标）
4. 计算 DX = |DI+ - DI-| / (DI+ + DI-) × 100
5. ADX = DX 的 14 日 EMA

**输出**：
- `adx_value`: ADX 值（0-100）
- `trend_strength`: "none" (<20) / "weak" (20-25) / "moderate" (25-40) / "strong" (40-60) / "extreme" (>60)
- `di_plus`: DI+ 值
- `di_minus`: DI- 值

**用途**：判断趋势是否值得跟踪（ADX > 25 表示有趋势）

### 3.3 Stochastic（随机指标）

**计算逻辑**：
```
%K = (收盘 - 最近14根最低) / (最近14根最高 - 最近14根最低) × 100
%D = %K 的 3 日 SMA
```

**输出**：
- `stoch_k`: %K 值（0-100）
- `stoch_d`: %D 值（0-100）
- `signal`: "overbought" (>80) / "oversold" (<20) / "neutral"

**用途**：捕捉反转点（超买/超卖区域）

### 3.4 CCI（商品通道指数）

**计算逻辑**：
```
典型价格 = (最高 + 最低 + 收盘) / 3
CCI = (典型价格 - SMA(20)) / (0.015 × 平均偏差)
```

**输出**：
- `cci_value`: CCI 值
- `signal`: "overbought" (>100) / "oversold" (<-100) / "neutral"

**用途**：识别周期性转折

### 3.5 Williams %R

**计算逻辑**：
```
%R = (最近14根最高 - 收盘) / (最近14根最高 - 最近14根最低) × -100
```

**输出**：
- `williams_r`: %R 值（-100 到 0）
- `signal`: "overbought" (>-20) / "oversold" (<-80) / "neutral"

**用途**：类似 Stochastic，但更敏感

### 3.6 VWAP（成交量加权平均价）

**计算逻辑**：
```
典型价格 = (最高 + 最低 + 收盘) / 3
VWAP = Σ(典型价格 × 成交量) / Σ(成交量)
```

**输出**：
- `vwap_value`: VWAP 值
- `price_vs_vwap`: 价格相对于 VWAP 的百分比偏差
- `position`: "above" / "below" / "at"

**用途**：机构成本参考，价格在 VWAP 上方偏多

### 3.7 综合信号 aggregate_signals()

**逻辑**：
1. 收集各指标信号（RSI、MACD、Stochastic、CCI、Williams %R、ADX、OBV）
2. 统计 bullish/bearish/neutral 信号数量
3. 加权计算综合得分（ADX 权重更高，因为它是趋势过滤器）
4. 输出：
   - `overall_signal`: "bullish" / "bearish" / "neutral"
   - `confidence`: 0.0-1.0（基于信号一致性）
   - `summary`: 人类可读摘要

---

## 4. Prompt 集成设计

### 4.1 扩展 ContextModule 渲染

在现有 RSI/MACD/Volume 基础上，新增指标摘要区块：

```
## 技术指标扩展
- OBV: 上升趋势（连续3日量价齐升）
- ADX: 28.5（中等趋势强度）
- Stochastic: %K=72, %D=68（接近超买区）
- CCI: 45（中性偏多）
- Williams %R: -35（中性）
- VWAP: 152.30（当前价格 155.20 在 VWAP 上方 +1.9%）
- 综合信号: bullish（置信度 0.65）
```

### 4.2 Prompt 长度控制

- 每个指标摘要约 1-2 行
- 总计增加约 10-15 行
- 现有 prompt 约 200 行，增幅 <10%，可接受

---

## 5. 测试与验证策略

### 5.1 单元测试

**文件**: `backend/tests/test_technical_indicators.py`

**测试用例**（每个指标至少 3 个）：
1. 正常数据：验证计算结果与参考值一致（精度 0.01）
2. 边界条件：数据不足时返回默认值
3. 异常数据：空列表、全零、NaN 处理

**参考值来源**：TradingView / Investopedia 公式

### 5.2 集成测试

**文件**: `backend/tests/test_data_aggregator.py`

**测试用例**：
1. Mock BrokerGateway 返回真实 K 线数据
2. 验证 `fetch_market_data()` 返回的指标字段完整
3. 验证 prompt 包含新指标摘要

### 5.3 回测验证

**方法**：
- 使用现有 BacktestEngine 对比"有/无新指标"的 LLM 决策质量
- 基线：当前 RSI/MACD/ATR/BB 组合
- 实验组：新增 OBV/ADX/Stochastic/CCI/Williams %R/VWAP

**指标**：
- 胜率
- 盈亏比
- 最大回撤
- 夏普比率

### 5.4 性能基准

**目标**：
- 指标计算耗时 < 50ms（120 根 K 线）
- Prompt 长度增幅 < 15%

### 5.5 验收标准

- [ ] `pytest` 全通过（现有 549 项 + 新增约 30 项）
- [ ] `basedpyright` 0 errors
- [ ] `npm run type-check` + `npm run build` 通过
- [ ] 回测对比显示新指标对决策质量有正向贡献（或至少不降低）

---

## 6. 实现计划

### 6.1 任务分解

| 任务 | 描述 | 预估工时 |
|------|------|----------|
| T1 | 实现 OBV 计算方法 + 测试 | 0.5 天 |
| T2 | 实现 ADX 计算方法 + 测试 | 0.5 天 |
| T3 | 实现 Stochastic 计算方法 + 测试 | 0.5 天 |
| T4 | 实现 CCI 计算方法 + 测试 | 0.5 天 |
| T5 | 实现 Williams %R 计算方法 + 测试 | 0.5 天 |
| T6 | 实现 VWAP 计算方法 + 测试 | 0.5 天 |
| T7 | 实现 aggregate_signals() + 测试 | 0.5 天 |
| T8 | 集成到 DataAggregator + ContextModule | 0.5 天 |
| T9 | 回测验证 + 性能基准 | 0.5 天 |
| **总计** | | **4.5 天** |

### 6.2 依赖关系

- T1-T6 可并行实现
- T7 依赖 T1-T6
- T8 依赖 T7
- T9 依赖 T8

---

## 7. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 指标计算错误 | LLM 决策质量下降 | 严格单元测试 + 参考值验证 |
| Prompt 长度过长 | API 成本增加 | 控制在 15% 以内 |
| 指标信号冲突 | LLM 困惑 | aggregate_signals() 提供综合判断 |
| 性能问题 | 响应延迟 | 基准测试 + 优化算法 |

---

## 8. 参考资料

- [Investopedia: OBV](https://www.investopedia.com/terms/o/onbalancevolume.asp)
- [Investopedia: ADX](https://www.investopedia.com/terms/a/adx.asp)
- [Investopedia: Stochastic](https://www.investopedia.com/terms/s/stochasticoscillator.asp)
- [Investopedia: CCI](https://www.investopedia.com/terms/c/commoditychannelindex.asp)
- [Investopedia: Williams %R](https://www.investopedia.com/terms/w/williamsr.asp)
- [Investopedia: VWAP](https://www.investopedia.com/terms/v/vwap.asp)
