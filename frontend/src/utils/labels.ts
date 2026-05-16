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
