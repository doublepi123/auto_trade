# LLM 智能区间调整系统设计文档

## 目标与范围

### 目标

通过 DeepSeek LLM 分析当前市场行情数据，自动为策略推荐买入/卖出价格区间，减少人工频繁手动调整策略参数的依赖。

### 范围

- 仅对 **FLAT（空仓）** 状态的策略自动应用 LLM 推荐的区间
- 对 **持有仓位 (LONG/SHORT)** 状态的策略，采用**渐进式平滑过渡**策略
- 由定时任务（4 小时一次）和波动率事件（5% 阈值）双重触发
- LLM 返回建议价格 + 置信度，后端结合风控规则做二次校验

### 非目标

- 不实现高频重新计算（不会每次报价都触发 LLM）
- 不处理多币种汇率复杂场景
- 不替代人工 Kill Switch 和风控判断

---

## 触发机制

| 触发器 | 条件 | 行为 |
|--------|------|------|
| **定时触发** | 每 4 小时执行一次（cron: `0 */4 * * *`） | 常规分析，产生新区间建议 |
| **波动率触发** | 当前价格距离上一次 LLM 触发时的价格单向波动 ≥ 5% | 立即触发额外分析 |
| **手动触发** | 用户在 Strategy 页面点击"重新分析"按钮 | 即时发起分析 |

**触发频率限制：** 两次连续 LLM 调用间隔至少 30 分钟（无论由何种触发器触发），避免过度 API 调用。

---

## LLM 输入数据构建

### 数据来源

| 数据类型 | 来源 | 说明 |
|----------|------|------|
| 最近 7 天 OHLCV | 长桥 SDK `history_candlestick` API | 日 K 线数据，权重 60% |
| 最近 24 小时分钟级数据 | 长桥 SDK `history_candlestick` API（1分钟粒度） | 权重 40% |
| 当前持仓状态 | 后端 StrategyService + RuntimeState | 当前仓位、盈亏 |
| 最近成交记录 | 后端 `orders` 表最近 N 条 | 买入均价、卖出均价 |
| 股票基本面摘要 | 长桥 SDK `static_info` API | 行业、PE 等基础信息 |
| 当前策略参数 | 数据库 `strategy_config` 表 | buy_low, sell_high, short_selling |
| 市场波动率指标 | 后端计算（基于 24 小时数据） | ATR (平均真实波幅)，布林带宽度 |

### 数据聚合（数据聚合器 DataAggregator）

```
输入：symbol, market
处理：
  1. 调长桥 API 获取历史和实时数据
  2. 调内部数据库获取策略状态
  3. 计算 ATR、布林带宽度等辅助指标
  4. 将所有数据序列化为 JSON 或 Markdown 表格格式
输出：聚合数据对象（LLM Prompt 可消费的文本格式）
```

### LLM Prompt 模板

```markdown
你是一个专业量化交易顾问。请基于以下市场数据，为区间交易策略推荐买入下限（buy_low）和卖出上限（sell_high）。

## 当前策略参数
- 标的: {symbol} ({market})
- 当前 buy_low: {current_buy_low}
- 当前 sell_high: {current_sell_high}
- 允许做空: {short_selling}

## 市场数据（最近 7 天日 K 线）
| 日期 | 开盘 | 最高 | 最低 | 收盘 | 成交量 |
|------|------|------|------|------|--------|
{ohlcv_table}

## 当前技术指标
- ATR(14): {atr}
- 布林带: 上轨 {bb_upper} / 中轨 {bb_middle} / 下轨 {bb_lower}
- 当前价格: {current_price}
- 波动率: {volatility_pct}%
- 当前持仓: {current_position_desc}
- 最近 3 笔成交: {recent_trades_summary}

## 请输出以下 JSON 格式：
{{
  "analysis": "简短的市场分析（50字以内）",
  "suggested_buy_low": 具体价格,
  "suggested_sell_high": 具体价格,
  "confidence_score": 0.0到1.0,
  "reasoning": "简要推理过程"
}}

注意：
1. sell_high 必须严格大于 buy_low
2. confidence_score >= 0.7 才建议采纳
3. 避免给出与现有持仓方向矛盾的区间
```

---

## 后端架构

### 组件图

```
┌─────────────────────────────────────────┐
│           Auto Trade AppRunner          │
│  (已有：行情订阅、引擎、风控、执行)          │
└──────────────────┬──────────────────────┘
                   │
                   │ 触发
                   ▼
┌─────────────────────────────────────────┐
│     CronJob / 波动率检测 / 手动触发       │
└──────────────────┬──────────────────────┘
                   │
                   │ 调度
                   ▼
┌─────────────────────────────────────────┐
│     LLMAdvisorService（LLM顾问服务）     │
│  · 触发频率控制 (30min 防抖)              │
│  · 调 DeepSeek API                      │
│  · 解析推荐结果 + 置信度校验               │
│  · 调 DataAggregator 构建 Prompt         │
└──────────────────┬──────────────────────┘
                   │
                   │ 推荐结果
                   ▼
┌─────────────────────────────────────────┐
│    IntervalApplicationService           │
│  (渐进式平滑过渡/直接生效)                │
└──────────────────┬──────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────┐
│    数据库 StrategyConfig (新字段)        │
│      · auto_interval_enabled            │
│      · llm_suggested_buy_low            │
│      · llm_suggested_sell_high          │
│      · llm_confidence_score             │
│      · llm_last_analysis_at             │
│      · llm_next_analysis_at             │
└─────────────────────────────────────────┘
```

### 核心新增模块

| 文件 | 职责 |
|------|------|
| `backend/app/services/llm_advisor_service.py` | LLM 调用、频率控制、结果解析 |
| `backend/app/services/data_aggregator.py` | 聚合长桥 API + 本地 DB 数据，生成 Prompt |
| `backend/app/services/interval_application_service.py` | 渐进式平滑过渡策略 + 风控校验 |
| `backend/app/crontabs/interval_analysis_cron.py` | 定时 Cron Job，调度 LLMAdvisorService |
| `backend/app/api/llm_advisor.py` | API 路由：GET/POST `/api/strategy/llm-interval` |

---

## 渐进式平滑过渡策略（核心交易逻辑）

当 LLM 分析完成并返回推荐区间时，后端按照当前策略持仓状态决定如何应用。

### 规则矩阵

| 当前持仓状态 | 新区间与现有持仓的关系 | 执行动作 |
|-------------|----------------------|---------|
| **FLAT（空仓）** | 任意 | 立即将 recommended 写入 `buy_low` / `sell_high` |
| **LONG（持多）** | `new_sell_high >= current_sell_high` | sell_high 取 max(old, new)，即止盈上移；buy_low 忽略 |
| **LONG（持多）** | `new_sell_high < current_sell_high` | sell_high 取 max(new, current_price * 1.05)，确保不立即触发反向卖出；buy_low 忽略 |
| **SHORT（持空）** | `new_buy_low <= current_buy_low` | buy_low 取 min(old, new)，即补仓下移；sell_high 忽略 |
| **SHORT（持空）** | `new_buy_low > current_buy_low` | buy_low 取 min(new, current_price * 0.95)，确保不立即触发反向回补；sell_high 忽略 |

### 风控兜底规则

即使 LLM 返回了建议，后端必须执行以下校验：

1. **sell_high > buy_low**：若 LLM 返回的 sell_high <= buy_low，直接丢弃。
2. **置信度 >= 0.7**：否则不推荐。
3. **sell_high 下限**：`sell_high >= current_price * 1.05`（避免立即触发卖出）。
4. **buy_low 上限**：`buy_low <= current_price * 0.95`（避免立即触发买入）。
5. **波动率上限**：新区间宽度（sell_high - buy_low）/ current_price 不应超过 20%，避免极端宽区间。

当上述任一规则被触发时，记录一条 `RiskEvent`：
```
event_type: "LLM_INTERVAL_REJECTED"
reason: "LLM 建议 sell_high ($190.00) 低于当前价格 ($200.00)，已被风控拒绝"
```

---

## 后端 API 设计

### 新 API：触发 LLM 分析

```
POST /api/strategy/llm-interval/analyze
```

**请求：**
```json
{
  "force": false
}
```

**响应：**
```json
{
  "success": true,
  "applied": false,
  "reason": "当前持仓 LONG，sell_high 已上调至 $230.00（LLM 建议 $235.00），buy_low 保持不变",
  "suggested_buy_low": 210.00,
  "suggested_sell_high": 235.00,
  "confidence_score": 0.85,
  "analysis": "近期 AAPL 震荡上行，ATR 扩大，建议适当上移区间...",
  "next_analysis_at": "2026-06-02T16:00:00Z",
  "applied_at": null
}
```

**错误响应：**
```json
{
  "success": false,
  "error": "LLM API 调用失败：timeout after 30s",
  "details": "..."
}
```

### 新 API：获取 LLM 分析状态

```
GET /api/strategy/llm-interval/status
```

**响应：**
```json
{
  "enabled": true,
  "last_analysis_at": "2026-06-02T12:00:00Z",
  "next_analysis_at": "2026-06-02T16:00:00Z",
  "current_suggestion": {
    "buy_low": 210.00,
    "sell_high": 235.00,
    "confidence_score": 0.85,
    "analysis": "..."
  },
  "applied_values": {
    "buy_low": 215.00,
    "sell_high": 230.00,
    "reason": "LONG 状态下 sell_high 已上调，buy_low 保持不变"
  }
}
```

### 新 API：启用/禁用自动区间

```
PUT /api/strategy/llm-interval/enable
PUT /api/strategy/llm-interval/disable
```

---

## 数据库变更

### StrategyConfig 表新增字段

```sql
-- 2026-06-02_add_llm_interval_fields.py

ALTER TABLE strategy_config ADD COLUMN auto_interval_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE strategy_config ADD COLUMN llm_suggested_buy_low FLOAT DEFAULT NULL;
ALTER TABLE strategy_config ADD COLUMN llm_suggested_sell_high FLOAT DEFAULT NULL;
ALTER TABLE strategy_config ADD COLUMN llm_confidence_score FLOAT DEFAULT NULL;
ALTER TABLE strategy_config ADD COLUMN llm_analysis TEXT DEFAULT NULL;
ALTER TABLE strategy_config ADD COLUMN llm_last_analysis_at DATETIME DEFAULT NULL;
ALTER TABLE strategy_config ADD COLUMN llm_next_analysis_at DATETIME DEFAULT NULL;
ALTER TABLE strategy_config ADD COLUMN llm_applied_buy_low FLOAT DEFAULT NULL;
ALTER TABLE strategy_config ADD COLUMN llm_applied_sell_high FLOAT DEFAULT NULL;
ALTER TABLE strategy_config ADD COLUMN llm_applied_at DATETIME DEFAULT NULL;
ALTER TABLE strategy_config ADD COLUMN llm_reject_reason TEXT DEFAULT NULL;
```

---

## 前端集成

### Strategy 页面变更

在 `frontend/src/views/Strategy.vue` 的现有表单上方增加 LLM 区间开关和分析卡片：

```vue
<template>
  <div>
    <h3>策略配置</h3>
    
    <!-- LLM 智能区间卡片 -->
    <el-card v-if="intervalStatus" style="max-width: 600px; margin-bottom: 20px;">
      <div style="display: flex; justify-content: space-between; align-items: center;">
        <h4>🤖 LLM 智能区间调整</h4>
        <el-switch
          v-model="intervalStatus.enabled"
          active-text="启用"
          inactive-text="禁用"
          @change="toggleAutoInterval"
        />
      </div>
      <div v-if="intervalStatus.current_suggestion" style="margin-top: 12px;">
        <p>最新分析: {{ intervalStatus.current_suggestion.analysis }}</p>
        <p>置信度: {{ intervalStatus.current_suggestion.confidence_score }}</p>
        <p>建议区间: {{ intervalStatus.current_suggestion.buy_low }} ~ {{ intervalStatus.current_suggestion.sell_high }}</p>
        <p v-if="intervalStatus.applied_values">
          已应用: {{ intervalStatus.applied_values.buy_low }} ~ {{ intervalStatus.applied_values.sell_high }}
          <span v-if="intervalStatus.applied_values.reason">({{ intervalStatus.applied_values.reason }})</span>
        </p>
        <p v-if="intervalStatus.current_suggestion.applied_at">
          上次应用: {{ formatTime(intervalStatus.current_suggestion.applied_at) }}
        </p>
      </div>
      <el-button size="small" :loading="analyzing" @click="triggerManualAnalysis">
        立即重新分析
      </el-button>
    </el-card>

    <!-- 原有策略表单... -->
  </div>
</template>
```

### Dashboard 页面变更

在现有的 status 展示中增加 LLM 状态指示器：

```vue
<template>
  <div v-if="status.llm_enabled" class="llm-indicator">
    <el-tag type="info" size="small">
      🤖 下次自动分析: {{ formatTime(status.llm_next_analysis_at) }}
    </el-tag>
    <el-tag v-if="status.llm_reject_reason" type="danger" size="small">
      上次分析被拒: {{ status.llm_reject_reason }}
    </el-tag>
  </div>
</template>
```

---

## 安全与风控

| 风险 | 缓解措施 |
|------|---------|
| LLM 幻觉导致异常区间 | 后端风控兜底规则：置信度 < 0.7 拒绝；sell_high >= price * 1.05；buy_low <= price * 0.95 |
| API Key 泄露 | DeepSeek API Key 存储在服务器 `.env`，前端无任何感知 |
| API 调用超时/失败 | 超时 30s，失败时静默重试一次，失败后发送 Server酱报警 "LLM 区间分析失败" |
| 过度频繁调用 | 30 分钟防抖窗口，无论多少触发器都不会突破 |
| LLM 建议导致亏损 | Kill Switch、风控模块对 LLM 建议不特殊豁免，仍然走 full risk check |

---

## 配置项

新增 `.env` 环境变量：

```env
# LLM 智能区间调整
DEEPSEEK_API_KEY=your-deepseek-api-key
# 可选，默认 https://api.deepseek.com/v1/chat/completions
DEEPSEEK_API_URL=  
LLM_INTERVAL_CRON_INTERVAL_MINUTES=240
LLM_INTERVAL_VOLATILITY_THRESHOLD_PCT=5
LLM_MIN_CONFIDENCE=0.7
LLM_MAX_STRIPE_WIDTH_PCT=20
```

---

## 测试策略

### 后端测试（pytest）

| 测试场景 | 验证点 |
|----------|--------|
| `test_llm_advisor_service_trigger_throttle` | 30 分钟防抖窗口 |
| `test_llm_advisor_service_parse_response` | LLM 返回 JSON 正确解析 |
| `test_llm_advisor_service_fallback_on_api_error` | API 超时后重试并报警 |
| `test_interval_application_flat_apply` | FLAT 状态下区间立即生效 |
| `test_interval_application_long_sell_high_higher` | LONG 状态下 sell_high 上调 |
| `test_interval_application_long_sell_high_lower` | LONG 状态下 sell_high 下调被风控兜底 |
| `test_interval_application_short_buy_lower` | SHORT 状态下 buy_low 下调 |
| `test_interval_application_short_buy_higher` | SHORT 状态下 buy_low 上调被兜底 |
| `test_risk_reject_llm_confidence_low` | 置信度 < 0.7 被拒 |
| `test_risk_reject_sell_high_near_price` | sell_high 离 current_price 太近被拒 |
| `test_risk_reject_buy_low_near_price` | buy_low 离 current_price 太近被拒 |
| `test_database_migration_new_fields` | Alembic 迁移正确添加新字段 |

### 前端测试（Cypress）

| 测试场景 | 验证点 |
|----------|--------|
| `strategy_llm_toggle.cy.ts` | LLM 开关能正常启用/禁用 |
| `strategy_llm_manual_analyze.cy.ts` | 点击"立即重新分析"发起 API 调用 |
| `strategy_llm_show_suggestion.cy.ts` | 分析结果显示正确（置信度、建议区间） |
| `strategy_llm_show_applied_values.cy.ts` | "已应用"提示和原因正确显示 |
| `dashboard_llm_indicator.cy.ts` | Dashboard 上的 LLM 状态指示器存在 |

### 集成测试

| 测试场景 | 验证点 |
|----------|--------|
| Docker Compose 启动后 `curl -X POST http://localhost:8000/api/strategy/llm-interval/analyze` | 返回正确结构；包含 confidence_score |
| 手动触发 2 次（间隔 < 30 分钟） | 第二次返回 `429 Too Many Requests` 或类似频率限制信息 |

---

## 实施估算

| 模块 | 文件 | 预计工时 |
|------|------|---------|
| 数据聚合器 | `data_aggregator.py` | 1.5h |
| LLM 顾问服务 | `llm_advisor_service.py` | 2h |
| 区间应用服务 | `interval_application_service.py` | 2.5h |
| Cron Job | `interval_analysis_cron.py` | 1h |
| API 路由 | `api/llm_advisor.py` | 1h |
| 数据库迁移 | Alembic migration | 0.5h |
| 前端 UI 适配 | `Strategy.vue`, `Dashboard.vue` | 2.5h |
| 后端测试 | pytest 测试文件 | 2.5h |
| 前端测试 | Cypress 测试文件 | 1.5h |
| 集成调试 | Docker + E2E | 1.5h |
| **总计** | | **~17h (~2工作日)** |

---

## 附录：状态机与 LLM 交互时序图

```
时间线 ──►

[T0] 策略启动 → engine.state = FLAT
[T1] price <= buy_low → BUY → engine.state = LONG
[T2= T1+2h] Cron 触发 LLM 分析：
   - LLM 建议 buy_low=195, sell_high=220 (confidence=0.85)
   - 引擎是 LONG → sell_high 取 max(old=210, new=220)=220
   - buy_low 被忽略
   → engine.state 仍为 LONG；策略参数: buy_low=200, sell_high=220
[T3] price >= 220 → SELL → engine.state = FLAT
[T4= T3+10min] FLAT 状态下，LLM 建议自动生效（如果已启用 auto_interval）
   → 策略参数: buy_low=195, sell_high=220
[T5] price <= 195 → BUY → engine.state = LONG
```

---

## Spec Self-Review

### 1. 占位符扫描
- [x] 无 "TBD", "TODO", "待补充": 所有值均有默认值或明确来源。
- [x] 无模糊需求: 所有校验、触发器、状态机均有明确数字。

### 2. 内部一致性检查
- [x] `buy_low` 必须 `<= current_price * 0.95`（兜底规则）
- [x] `sell_high` 必须 `>= current_price * 1.05`（兜底规则）
- [x] 防抖窗口 `30` 分钟与定时 `4` 小时不冲突；波动率事件 `5%` 是额外触发器。
- [x] LONG 状态下 `buy_low` 被忽略，与 SHORT 状态下 `sell_high` 被忽略对称。
- [x] 数据聚合器的"ATR、布林带"已在前端 UI 和数据流中被引用，后端实现时保持一致。

### 3. 范围检查
- [x] 本次设计聚焦单一功能：LLM 智能区间推荐。
- [x] 不涉及回测、监控面板、审计日志等已在 Roadmap 的其他迭代中的功能。

### 4. 歧义检查
- [x] "持有仓位时使用渐进式平滑过渡" 已明确为：LONG 调整 sell_high，SHORT 调整 buy_low。
- [x] "风控兜底规则" 已明确为 5 条具体阈值校验，无模糊语言。
- [x] API 响应示例已明确 `applied: false` 时仍然返回 `reason`。

---

*文档已完成自审查，可以进入实施阶段。*
