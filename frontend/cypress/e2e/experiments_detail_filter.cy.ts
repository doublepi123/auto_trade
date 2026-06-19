describe('Experiments Run Detail, Status Filter & Page Export', () => {
  beforeEach(() => {
    cy.visitApp('/#/experiments')
    // Register overrides AFTER visitApp so they take precedence over the
    // support stub's generic `/api/strategy-experiments/*/runs*` route.
    cy.intercept('POST', '/api/strategy-experiments', {
      body: { id: 1, name: 'Test', symbol: 'AAPL.US', status: 'COMPLETED', estimated_runs: 3, completed_runs: 2, failed_runs: 1, created_at: '2026-05-01T10:00:00Z' },
    }).as('createExp')
    cy.intercept('POST', '/api/strategy-experiments/1/run', {
      body: { id: 1, name: 'Test', symbol: 'AAPL.US', status: 'COMPLETED', estimated_runs: 3, completed_runs: 2, failed_runs: 1, created_at: '2026-05-01T10:00:00Z' },
    }).as('runExp')
    cy.intercept('GET', '/api/strategy-experiments/1/runs*', {
      body: {
        items: [
          { id: 1, experiment_id: 1, parameters: { buy_low: 178.0, sell_high: 190.0, fee_rate: 0.0005 }, status: 'COMPLETED', total_pnl: 120.5, total_return_pct: 0.12, max_drawdown_pct: 0.02, win_rate: 0.5, trade_count: 2, closed_trade_count: 2, sharpe_ratio: 0.55, profit_factor: 1.2, profit_loss_ratio: 1.5, error: null, created_at: '2026-05-01T10:00:00Z' },
          { id: 2, experiment_id: 1, parameters: { buy_low: 180.0, sell_high: 195.0, fee_rate: 0.0005 }, status: 'FAILED', total_pnl: 0, total_return_pct: 0, max_drawdown_pct: 0, win_rate: 0, trade_count: 0, closed_trade_count: 0, sharpe_ratio: null, profit_factor: null, profit_loss_ratio: null, error: 'csv parse error', created_at: '2026-05-01T10:01:00Z' },
        ],
        total: 2,
        page: 1,
        page_size: 20,
      },
    }).as('listRuns')

    cy.get('[data-testid="exp-csv"]').type('timestamp,open,high,low,close,volume\n2026-05-01T09:30:00Z,180,181,179,180.5,1000')
    cy.get('[data-testid="exp-run-btn"]').click()
    cy.wait('@createExp')
    cy.wait('@runExp')
    cy.wait('@listRuns')
  })

  it('expands a run to show full parameters and error', () => {
    cy.get('[data-testid="leaderboard-table"] .el-table__expand-icon').first().click()
    cy.get('[data-testid="run-detail"]').should('contain', 'buy_low')
    cy.get('[data-testid="run-detail"]').should('contain', '178')
  })

  it('filters runs by status within the loaded page', () => {
    cy.get('[data-testid="exp-status-all"]').should('contain', '2')
    cy.get('[data-testid="exp-status-done"]').should('contain', '1')
    cy.get('[data-testid="exp-status-failed"]').should('contain', '1')

    cy.get('[data-testid="exp-status-failed"]').click()
    cy.get('[data-testid="run-pnl"]').should('have.length', 1)
    cy.get('[data-testid="leaderboard-table"]').should('contain', 'csv parse error')

    cy.get('[data-testid="exp-status-done"]').click()
    cy.get('[data-testid="run-pnl"]').should('have.length', 1)
  })

  it('exports the currently loaded page as CSV', () => {
    cy.get('[data-testid="exp-export-page-csv"]').should('not.be.disabled').click()
    cy.document().its('body').should('contain', '已导出当前页')
  })
})
