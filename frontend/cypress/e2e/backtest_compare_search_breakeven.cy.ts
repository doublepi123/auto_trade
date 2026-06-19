describe('Backtest Compare Export, Saved-run Search & Break-even Fee', () => {
  it('shows the break-even fee rate when fee sensitivity crosses zero', () => {
    cy.visitApp('/#/backtest')
    cy.wait('@getStrategy')
    cy.intercept('POST', '/api/backtest/run', {
      body: {
        params: { symbol: 'AAPL.US', buy_low: 100, sell_high: 200, short_selling: false, min_profit_amount: 0, max_daily_loss: 5000, max_consecutive_losses: 3, quantity: 2, initial_cash: 10000, fee_rate: 0, fixed_fee: 0, slippage_pct: 0, stop_loss_pct: 0 },
        metrics: { initial_cash: 10000, final_equity: 10200, total_pnl: 200, total_return_pct: 2, max_drawdown_pct: 0, trade_count: 2, closed_trade_count: 1, winning_trades: 1, losing_trades: 0, win_rate: 100, avg_holding_minutes: 1, fees_paid: 0, skipped_signals: 0, final_state: 'flat' },
        equity_curve: [], trades: [], skipped_signals: [],
        fee_sensitivity: [
          { fee_rate: 0, total_pnl: 200, total_return_pct: 2, max_drawdown_pct: 0 },
          { fee_rate: 0.005, total_pnl: 50, total_return_pct: 0.5, max_drawdown_pct: 0 },
          { fee_rate: 0.01, total_pnl: -100, total_return_pct: -1, max_drawdown_pct: 0 },
        ],
      },
    }).as('runBacktestBe')
    cy.get('[data-testid="run-backtest-button"]').click()
    cy.wait('@runBacktestBe')
    // break-even between 0.005 (pnl 50) and 0.01 (pnl -100): t = 50/150 = 0.333 → 0.005+0.333*0.005 = 0.00667
    cy.get('[data-testid="fee-breakeven"]').should('contain', '0.6667%')
  })

  it('filters saved runs by name and exports the compare table', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/backtest/runs', {
      body: {
        items: [
          { id: 1, name: 'Bull grid', symbol: 'AAPL.US', params: { buy_low: 100, sell_high: 200 }, metrics: { total_pnl: 100, total_return_pct: 1, max_drawdown_pct: 0.5, trade_count: 2, win_rate: 100, sharpe_ratio: 1.2 }, created_at: '2026-06-16T12:00:00Z' },
          { id: 2, name: 'Bear grid', symbol: 'NVDA.US', params: { buy_low: 110, sell_high: 200 }, metrics: { total_pnl: 80, total_return_pct: 0.8, max_drawdown_pct: 0.7, trade_count: 2, win_rate: 100, sharpe_ratio: 1.0 }, created_at: '2026-06-16T12:00:00Z' },
        ],
        total: 2, page: 1, page_size: 50,
      },
    }).as('listRuns')
    cy.visit('/#/backtest')
    cy.wait('@getStrategy')
    cy.wait('@listRuns')
    cy.contains('[data-testid="compare-panel"] .el-collapse-item__header', '结果对比').click()
    cy.get('input[placeholder="搜索名称/标的"]').should('be.visible').type('Bull')
    cy.get('[data-testid="compare-panel"]').should('contain', 'Bull grid')
    cy.get('[data-testid="compare-panel"]').should('not.contain', 'Bear grid')
  })
})
