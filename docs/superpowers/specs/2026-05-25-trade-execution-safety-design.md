# 交易执行安全与成本控制设计

> **状态：** 已确认设计（2026-05-25）
> **范围：** Roadmap P4 - 实盘费用后收益保护、撤单前改价校验、LLM 执行节流、跳过原因展示。
> **决策：** API 鉴权收紧（审计项 P2）保留审计记录，但在可信内网部署假设下明确不实施。

## 1. 目标与范围

系统当前已经具备区间策略、pending 订单对账、持久化入场成本、最低平仓盈利检查，以及回测费用模型。P4 不改变 `flat / long / short` 状态机，只在真实下单周边增加保护规则：

1. 普通盈利平仓必须在扣除预估双边手续费后仍满足 `min_profit_amount`。
2. LLM 的 `CANCEL_REPLACE` 必须先证明替代价格有意义，再撤销已有挂单。
3. LLM 主动发单不能在短时间内重复发起同一券商方向的订单。
4. Dashboard 与 Decision Timeline 能解释订单为何被跳过。

### 非目标

- 不实施 P2 API 鉴权收紧。
- 不新增交易节假日日历或通用交易时段下单守卫。
- 不新增 broker retry/backoff。
- 不使用实盘费率配置替换现有回测费用/滑点模型。
- 不改变仓位 sizing、订单状态流转或止损优先级。

## 2. 当前基线

| 能力 | 当前行为 | P4 处理 |
|---|---|---|
| 风控与 pending 防并发 | `TradeExecutionService.execute()` 已拒绝危险或并发提交 | 保留行为，统一补跳过分类 |
| 平仓盈利保护 | 多空退出将毛利与 `max(min_exit_profit_pct, min_profit_amount)` 比较 | 普通平仓先扣预估实盘费用 |
| 止损退出 | `allow_loss_exit=True` 绕过盈利保护 | 保持不变，费用不得阻断止损 |
| Engine cooldown | 行情触发由 `StrategyEngine` 固定冷却 60 秒 | 保持不变 |
| LLM 订单动作 | 存在 pending 时可先撤单后发新单 | 撤单前增加校验 |
| 回测成本 | 已支持 `fee_rate`、`fixed_fee`、`slippage_pct` 和净收益过滤 | 保留，只补跳过分类 |
| 跳过事件 | 已有 `ORDER_SKIPPED`，无稳定原因分类 | 新增 `skip_category` payload |

## 3. 设计决策

### 3.1 实盘费用后平仓门槛

`StrategyConfig` 新增两个设置：

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `fee_rate_us` | float，`0..0.01` | `0.0005` | 美股单边交易费用预估率 |
| `fee_rate_hk` | float，`0..0.02` | `0.0030` | 港股单边交易费用预估率 |

新增 `backend/app/core/fees.py`，提供纯 `Decimal` 计算函数：

```python
def estimate_round_trip_fee(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    one_side_rate: Decimal,
) -> Decimal:
    return (entry_price + exit_price) * quantity * one_side_rate
```

费用保护继续由 `TradeExecutionService._profit_guard_for_exit()` 持有，因为该处已经拥有平仓数量、tracked 平均入场成本和止损绕过条件。对普通 `SELL` / `BUY_TO_COVER`：

```text
预期毛利 - 预估双边手续费 >= 要求盈利
要求盈利 = max(百分比盈利缓冲, min_profit_amount)
```

被拒绝时沿用 `ORDER_SKIPPED`，写入 `skip_category="FEE"` 以及毛利、预估费用、费用后收益和最低要求。`allow_loss_exit=True` 与当前一样直接绕过该规则。

### 3.2 撤单前改价校验

`StrategyConfig` 新增设置：

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `min_repricing_pct` | float，`0..0.05` | `0.003` | LLM 可撤单重挂所需的最低价格变化百分比 |

规则放在 `AppRunner.execute_llm_order_decision()`：该处在调用 `cancel_pending_order()` 之前同时持有现有 `_pending_order` 与 LLM 的替代请求。

对显式 `CANCEL_REPLACE` 以及隐式替换 pending 的 LLM 动作：

1. 解析替代动作和拟提交价格。
2. 在撤单前与当前 pending 价格比较。
3. 若 `abs(new_price - old_price) / old_price < min_repricing_pct`，不撤单、不提交，记录 `ORDER_SKIPPED / REPRICING`。
4. 仅当价格变化足够时，继续现有的撤单再提交流程。

若替代价格缺失或非法，继续走既有校验/失败路径；P4 不生成推测价格。

### 3.3 仅针对 LLM 的同方向执行冷却

`StrategyConfig` 新增设置：

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `llm_action_cooldown_seconds` | int，`0..3600` | `60` | 同标的同券商方向的 LLM 发单最小间隔 |

该冷却由 `AppRunner` 持有，并在 `execute_llm_order_decision()` 中于撤单或提交之前检查；实际提交成功后的时间记录可由 `_execute_llm_trade_action()` 返回结果后完成。方向按向券商提交的 side 归类：

- `BUY` 与 `BUY_TO_COVER` 归为 `BUY`。
- `SELL` 与 `SELL_SHORT` 归为 `SELL`。

只有订单进入 `SUBMITTED`、`PARTIAL_FILLED` 或 `FILLED` 才记录最近执行时间；拒绝或跳过不占用冷却窗口。

行情驱动的普通区间触发仍仅受现有 engine cooldown 管理，不叠加本规则，以免安全迭代顺带改变策略触发节奏。

替换请求依次通过改价阈值和 LLM 冷却检查；两项都必须在撤销现有挂单前完成，任何拒绝都保留原挂单。

### 3.4 跳过原因分类

扩展 `TradeEvent.payload` 中 `ORDER_SKIPPED` 的契约：

| `skip_category` | 产生条件 |
|---|---|
| `FEE` | 普通平仓未通过费用后净收益门槛 |
| `REPRICING` | LLM 替换订单的价格变化不足 |
| `COOLDOWN` | LLM 在冷却窗口内重复同方向发单 |
| `RISK` | 现有 `risk.check()` 拒绝执行 |
| `PENDING` | 非替换提交被现有 pending 阻止 |
| `POSITION` | 平仓路径没有可用持仓数量 |

各分类只携带所需的 JSON-safe 明细；前端以 `skip_category` 为稳定展示键，详情字段缺失时安全降级。

## 4. 组件与数据流

### 后端改动

| 区域 | 职责 |
|---|---|
| `backend/app/core/fees.py` | `Decimal` 费用估算及按市场选择配置费率 |
| `backend/app/services/trade_execution_service.py` | 应用费用后平仓门槛；为 risk、pending、position、fee 跳过补分类 |
| `backend/app/runner.py` | 在 `execute_llm_order_decision()` 中于撤单/提交前执行 LLM 改价与冷却 gate；维护冷却时间；记录跳过事件 |
| `backend/app/models.py`、`backend/app/schemas.py`、`backend/app/services/strategy_service.py` | 持久化并暴露四项 P4 设置 |
| `backend/app/database.py` | 增加 `_ensure_strategy_config_trade_safety_columns()` 以升级生产 SQLite |

### 执行流程

```text
行情触发动作
  -> StrategyEngine 既有 cooldown
  -> TradeExecutionService 的 risk / pending 检查
  -> 仅退出动作：费用后盈利保护
  -> 提交 broker 或记录 ORDER_SKIPPED

LLM 主动动作
  -> 如已有 pending：撤单前检查替代价格价值
  -> 撤单/提交前检查 LLM 同方向冷却
  -> 既有 engine state 调整与 TradeExecutionService 流程
  -> 提交 broker 或记录 ORDER_SKIPPED
```

### 数据库字段

`StrategyConfig` 新增：

| 字段 | 默认值 |
|---|---|
| `fee_rate_us` | `0.0005` |
| `fee_rate_hk` | `0.0030` |
| `min_repricing_pct` | `0.003` |
| `llm_action_cooldown_seconds` | `60` |

所有字段通过已有 `PUT /api/strategy` 保存。费率或 `min_repricing_pct` 设为 `0` 可禁用对应新增限制；`llm_action_cooldown_seconds=0` 仅禁用 LLM 发单冷却。

## 5. 前端行为

### Strategy

在现有策略表单增加“成本与执行保护”区域：

- 美股单边预估费率（百分比展示）。
- 港股单边预估费率（百分比展示）。
- LLM 同方向发单冷却秒数。
- LLM 最小改价百分比。

设置随现有策略 API 保存及回读。

### Decision Timeline

`ORDER_SKIPPED` 行显示分类标签，并对当前已加载页提供客户端筛选：

| Payload 值 | 展示文案 |
|---|---|
| `FEE` | 成本不足 |
| `REPRICING` | 改价不显著 |
| `COOLDOWN` | LLM 冷却中 |
| `RISK` | 风控阻断 |
| `PENDING` | 已有挂单 |
| `POSITION` | 可用持仓不足 |

### Dashboard

在已有最近事件列表中，为 `ORDER_SKIPPED` 摘要下方显示相同的原因标签；本轮不新增 Dashboard 控制流程。

## 6. 回测边界

`BacktestEngine` 已计算入场/退出费用、固定费用、滑点、费用敏感性和净收益门槛。P4 不将回测改为读取 `fee_rate_us` / `fee_rate_hk`，也不替换其试算参数。

本轮回测只扩展 `BacktestSkippedSignal` 的可选分类（例如 `FEE`），让模拟费用导致的跳过结果能够与实盘原因展示保持一致。

## 7. 测试与验证

### 后端

| 测试区域 | 覆盖内容 |
|---|---|
| `tests/test_fees.py` | Decimal 费用计算、US/HK 配置费率选择 |
| `tests/test_trade_execution_service.py` | 多空退出按费用后收益判断；止损绕过费用；risk/pending/position/fee 分类 |
| `tests/test_runner.py` | 改价不足保留 pending；变化足够才撤单重挂；LLM 冷却在撤单前拒绝并记录分类 |
| `tests/test_api.py`、`tests/test_models.py`、`tests/test_database.py` | 四项设置的校验、持久化与 SQLite 补列 |
| `tests/test_backtest.py` | 既有费用行为不变，费用跳过可返回分类 |

### 前端

| 测试区域 | 覆盖内容 |
|---|---|
| Strategy Cypress | 加载、编辑、保存、重载四项安全设置 |
| Decision Timeline Cypress | 渲染并筛选 `skip_category` |
| Dashboard Cypress | 最近跳过事件显示原因标签 |

### 交付验证

- `cd backend && python3 -m pytest tests/ -v`
- `python3 -m basedpyright`
- `cd frontend && npm run type-check && npm run build`
- 执行相关 Cypress E2E specs。
- 在不会实际下单的环境中验证：极小 LLM 改价记录 `REPRICING` 且不会撤销 pending。

## 8. 文档维护

- 在 `docs/Roadmap.md` 中保留 P2 的审计发现，并记录 2026-05-25 owner decision：明确不实施。
- 将 P4 标为当前下一迭代并链接本设计。
- 在 `CLAUDE.md` 中记录可信内网与 P2 非目标决策，避免后续再次误列为待办。
- `README.md` 仅在 P4 实现交付且新增设置真实可用后更新，不提前宣称未交付能力。

## 9. 交付估算

| 工作流 | 预计工时 |
|---|---|
| 后端费用保护、LLM gate、设置、迁移及测试 | 1.5~2 天 |
| 前端设置与原因展示、Cypress | 1 天 |
| 文档与端到端验证 | 0.5 天 |
| **合计** | **3~3.5 天** |

## 10. 延后风险

P4 后仍保留以下独立主题：

- 交易时段下单守卫（含节假日历）。
- Broker retry/backoff 与限流结构化处理。
- 服务重启时已有 broker pending 订单的集成验证。
- API 鉴权收紧（P2），已在当前可信内网部署模型下明确拒绝实施。
