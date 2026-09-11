export function engineStateLabel(state?: string | null): string {
  switch (state?.toLowerCase()) {
    case 'flat':
      return '空仓'
    case 'long':
      return '持仓'
    case 'short':
      return '做空'
    default:
      return '未知'
  }
}

export function marketLabel(market?: string | null): string {
  switch (market) {
    case 'US':
      return '美股'
    case 'HK':
      return '港股'
    default:
      return '未知市场'
  }
}

export function orderSideLabel(side?: string | null): string {
  switch (side) {
    case 'BUY':
      return '买入'
    case 'SELL':
      return '卖出'
    case 'SELL_SHORT':
      return '开空'
    case 'BUY_TO_COVER':
      return '平空'
    default:
      return '未知方向'
  }
}

export function orderStatusLabel(status?: string | null): string {
  switch (status) {
    case 'SUBMITTED':
      return '已提交'
    case 'FILLED':
      return '已成交'
    case 'PARTIAL_FILLED':
      return '部分成交'
    case 'REJECTED':
      return '已拒绝'
    case 'CANCELLED':
      return '已取消'
    case 'SKIPPED':
      return '已跳过'
    default:
      return '未知状态'
  }
}

export function positionSideLabel(side?: string | null): string {
  switch (side) {
    case 'LONG':
      return '多头'
    case 'SHORT':
      return '空头'
    default:
      return '未知'
  }
}

export function skipCategoryLabel(category?: string | null): string {
  switch (category) {
    case 'FEE': return '成本不足'
    case 'REPRICING': return '改价不显著'
    case 'COOLDOWN': return '冷却或频次限制'
    case 'REGIME': return '市场状态阻断'
    case 'RISK': return '风控阻断'
    case 'DRAWDOWN': return '回撤限制'
    case 'PENDING': return '已有挂单'
    case 'POSITION': return '可用持仓不足'
    case 'SESSION': return '非交易时段'
    default: return ''
  }
}

export function auditActionLabel(action?: string | null): string {
  switch (action) {
    case 'START': return '启动运行'
    case 'STOP': return '停止运行'
    case 'PAUSE': return '暂停'
    case 'RESUME': return '恢复'
    case 'KILL_SWITCH': return '紧急停止'
    case 'DISABLE_KILL_SWITCH': return '解除紧急停止'
    case 'STRATEGY_UPDATE': return '策略更新'
    case 'CREDENTIALS_UPDATE': return '凭证更新'
    case 'ORDER_CANCEL': return '撤单'
    case 'TRADING_SESSION_BLOCKED': return '时段拦截'
    case 'BROKER_RETRY': return '券商重试'
    default: return action || '审计事件'
  }
}

export function tradeEventTypeLabel(eventType?: string | null): string {
  switch (eventType) {
    case 'LLM_ANALYSIS':
      return 'LLM 分析'
    case 'ORDER_SUBMITTED':
      return '已下单'
    case 'ORDER_SYNCED':
      return '订单同步'
    case 'ORDER_FILLED':
      return '订单成交'
    case 'ORDER_CANCELLED':
      return '订单撤销'
    case 'ORDER_REJECTED':
      return '订单拒绝'
    case 'ORDER_SKIPPED':
      return '订单跳过'
    case 'ORDER_STATUS_CHANGED':
      return '订单更新'
    case 'RISK_PAUSED':
      return '风控暂停'
    case 'RISK_AUTO_RESUMED':
      return '自动恢复'
    case 'PRIMARY_SWITCHED':
      return '主标的切换'
    case 'PRIMARY_SWITCH_ROLLED_BACK':
      return '主标的切换回滚'
    case 'PRIMARY_SWITCH_BLOCKED':
      return '主标的切换拦截'
    default:
      return eventType || '事件'
  }
}

export function alertRuleTypeLabel(ruleType?: string | null): string {
  switch (ruleType) {
    case 'price_above':
      return '价格上穿 ≥'
    case 'price_below':
      return '价格下穿 ≤'
    case 'daily_loss':
      return '日内亏损 ≤'
    case 'consecutive_losses':
      return '连续亏损 ≥'
    case 'kill_switch_engaged':
      return '熔断开关触发'
    case 'interval_stale':
      return '区间失效 ≥'
    case 'trading_dormant':
      return '静默未开仓 ≥'
    case 'margin_risk_level':
      return '保证金风控等级 ≥'
    case 'margin_call':
      return '追缴保证金 ≥'
    default:
      return ruleType || '未知类型'
  }
}

export function candidacyVerdictLabel(verdict?: string | null): string {
  switch (verdict) {
    case 'NO_SELECTION_RUN':
      return '无候选池运行'
    case 'SELECTION_SUPPORTED':
      return '证据支持切换'
    case 'SELECTION_NOT_SUPPORTED_BY_EVIDENCE':
      return '证据不支持切换'
    default:
      return '未知判定'
  }
}

export function candidacyIncumbentStatusLabel(status?: string | null): string {
  switch (status) {
    case 'EVIDENCE_THIN':
      return '证据不足'
    case 'ACCEPTABLE':
      return '仍可接受'
    case 'TREND_UNSUITABLE':
      return '趋势不适配'
    default:
      return '未知状态'
  }
}

export function poolGateStatusLabel(status?: string | null): string {
  switch (status) {
    case 'PASS':
      return '已通过'
    case 'BLOCKED':
      return '已阻断'
    case 'UNASSESSABLE':
      return '无法评估'
    default:
      return '未知状态'
  }
}

export function powerVerdictLabel(verdict?: string | null): string {
  switch (verdict) {
    case 'POWERED':
      return '样本量充足'
    case 'UNPOWERED':
      return '样本量不足'
    case 'UNMEASURABLE':
      return '无法测量'
    default:
      return '未知功效'
  }
}

export function candidateGateReasonLabel(reason?: string | null): string {
  switch (reason) {
    case 'IS_INCUMBENT':
      return '在任标的'
    case 'NOT_IN_POOL':
      return '不在候选池'
    case 'RANGE_VERDICT_NOT_SUITABLE':
      return '区间判定不适配'
    case 'TREND_ABOVE_CEILING':
      return '趋势占比超上限'
    case 'NO_REFERENCE_PRICE':
      return '无参考价'
    case 'REFERENCE_STALE':
      return '参考价过期'
    case 'REACH_EVIDENCE_ABSENT':
      return '无 reach 证据'
    case 'REACH_BELOW_TRADE_FLOOR':
      return '闭合交易数不足'
    case 'REACH_BELOW_RATE_FLOOR':
      return 'reach 率低于下限'
    default:
      return reason || '未知门闸'
  }
}

export function candidacyWithheldReasonLabel(reason?: string | null): string {
  switch (reason) {
    case 'POOL_SIGNAL_EDGE_BLOCKED':
      return '池级信号 edge 未通过'
    case 'UNPOWERED':
      return '样本量不足'
    case 'NO_GATE_PASSER':
      return '无标的通过闸门'
    case 'NO_SELECTION_RUN':
      return '无候选池运行'
    case 'SWITCH_DISABLED':
      return '自动切换未开启'
    default:
      return reason || '未知原因'
  }
}

export function marginRiskLevelLabel(level?: number | null): string {
  switch (level) {
    case 0:
      return '安全'
    case 1:
      return '中风险'
    case 2:
      return '预警'
    case 3:
      return '危险'
    default:
      return '未知'
  }
}
