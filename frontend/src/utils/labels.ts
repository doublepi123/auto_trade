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
    default:
      return eventType || '事件'
  }
}
