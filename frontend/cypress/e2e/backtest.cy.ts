describe('Backtest', () => {
  beforeEach(() => {
    cy.visitApp('/#/backtest')
    cy.contains('回测', { timeout: 10000 }).should('be.visible')
  })

  it('renders backtest inputs and actions', () => {
    cy.contains('参数').should('be.visible')
    cy.contains('历史数据').should('be.visible')
    cy.get('[data-testid="backtest-csv-input"]').should('be.visible')
    cy.get('[data-testid="run-backtest-button"]').should('be.visible')
  })

  it('runs backtest and displays metrics, charts, and trades', () => {
    cy.get('[data-testid="run-backtest-button"]').click()
    cy.wait('@runBacktest')

    cy.get('[data-testid="backtest-metrics"]').should('be.visible')
    cy.contains('总收益').should('be.visible')
    cy.contains('+$200.00').should('be.visible')
    cy.get('[data-testid="backtest-chart"]').should('be.visible')
    cy.get('[data-testid="backtest-trade-marker"]').should('exist')
    cy.get('[data-testid="backtest-trades"]').should('be.visible')
    cy.contains('买入').should('be.visible')
    cy.contains('卖出').should('be.visible')
    cy.contains('费用敏感性').should('be.visible')
    cy.contains('成本不足').should('be.visible')
  })

  it('can load sample csv after edits', () => {
    cy.get('[data-testid="backtest-csv-input"]').clear().type('bad')
    cy.contains('载入示例').click()
    cy.get('[data-testid="backtest-csv-input"]').should('contain.value', 'timestamp,open,high,low,close,volume')
  })

  it('runs a parameter sweep and renders ranked table + heatmap', () => {
    cy.get('[data-testid="sweep-panel"]').should('be.visible')
    cy.get('[data-testid="run-sweep-button"]').click()
    cy.wait('@runBacktestSweep')

    cy.get('[data-testid="sweep-results-table"]').should('be.visible')
    cy.contains('排名（Top 4）').should('be.visible')
    cy.get('[data-testid="sweep-heatmap"]').should('be.visible')
    cy.get('[data-testid="sweep-heatmap"] .heatmap-table').should('exist')
  })

  it('runs a walk-forward evaluation', () => {
    // Expand the (collapsed by default) walk-forward panel, then run.
    cy.contains('Walk-Forward 滚动窗口').click()
    cy.get('[data-testid="run-walkforward-button"]').click()
    cy.wait('@runWalkForward')
    cy.get('[data-testid="wf-windows-table"]').should('be.visible')
    cy.contains('逐窗口样本外表现').should('be.visible')
  })

  it('runs a what-if stress ensemble', () => {
    cy.contains('What-If 压力测试').click()
    cy.get('[data-testid="run-stress-button"]').click()
    cy.wait('@runStressTest')
    cy.get('[data-testid="stress-summary"]').should('be.visible')
  })

  it('saves a run and compares', () => {
    // Run a backtest first so a result exists to save.
    cy.get('[data-testid="run-backtest-button"]').click()
    cy.wait('@runBacktest')

    cy.contains('结果对比').click()
    cy.get('[data-testid="compare-save-name"]').type('My Run')
    cy.get('[data-testid="compare-save-button"]').click()
    cy.wait('@saveBacktestRun')
    cy.contains('已保存').should('be.visible')
  })

  it('fetches candles from the market into the CSV box', () => {
    cy.get('[data-testid="candle-symbol"] input').clear().type('AAPL.US')
    cy.get('[data-testid="candle-fetch"]').click()
    cy.wait('@getBrokerCandles')
    cy.get('[data-testid="backtest-csv-input"]').should('contain.value', '2026-06-14T13:30:00Z')
  })
})
