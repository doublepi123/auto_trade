describe('Reports Consistency & Local Export', () => {
  function daysAgo(days: number): string {
    const date = new Date()
    date.setDate(date.getDate() - days)
    const y = date.getFullYear()
    const m = String(date.getMonth() + 1).padStart(2, '0')
    const d = String(date.getDate()).padStart(2, '0')
    return `${y}-${m}-${d}`
  }

  beforeEach(() => {
    cy.stubApi()
    cy.visit('/#/reports')
  })

  function runReport() {
    cy.intercept('GET', '/api/reports/range*', {
      statusCode: 200,
      body: {
        period_type: 'range',
        symbol: 'AAPL.US',
        start_date: '2024-01-15',
        end_date: '2024-01-17',
        metrics: {
          total_pnl: 425, total_trades: 4, win_count: 3, loss_count: 1, win_rate: 0.75,
          profit_loss_ratio: 3.2, avg_pnl_per_trade: 106.25, max_profit: 300, max_loss: -120,
          max_drawdown: 120, llm_suggestions_count: 0, llm_applied_count: 0, llm_apply_rate: 0,
          llm_profitable_count: 0, llm_accuracy_rate: 0,
        },
        daily_points: [
          { date: '2024-01-15', pnl: 300, cumulative_pnl: 300, drawdown: 0, trade_count: 2, win_count: 2 },
          { date: '2024-01-16', pnl: -120, cumulative_pnl: 180, drawdown: 120, trade_count: 1, win_count: 0 },
          { date: '2024-01-17', pnl: 245, cumulative_pnl: 425, drawdown: 0, trade_count: 1, win_count: 1 },
        ],
        attribution: [
          { key: 'SELL', label: '平多', trade_count: 3, pnl: 545, win_rate: 0.6667, share: 0.75 },
        ],
        details: [],
      },
    }).as('getReport')

    cy.get('[data-testid="reports-symbol-input"] input').clear().type('AAPL.US')
    cy.get('[data-testid="reports-from-date"] input').clear().type('2024-01-15')
    cy.get('[data-testid="reports-to-date"] input').clear().type('2024-01-17')
    cy.get('[data-testid="reports-search"]').click()
    cy.wait('@getReport')
  }

  it('derives a daily pnl consistency insight from loaded daily_points', () => {
    runReport()
    cy.get('[data-testid="reports-consistency"]').should('be.visible')
    // mean of [300, -120, 245] = 141.67
    cy.get('[data-testid="reports-consistency"]').should('contain', '141.67')
    cy.get('[data-testid="reports-consistency"]').should('contain', '稳定性')
  })

  it('local export button is enabled after a report loads and exports daily detail', () => {
    runReport()
    cy.get('[data-testid="reports-export-local-csv"]').should('not.be.disabled').click()
    cy.document().its('body').should('contain', '已本地导出每日明细')
  })

  it('daily table columns are sortable', () => {
    runReport()
    // sortable columns render a caret button; click the 盈亏 column sort header.
    cy.get('[data-testid="reports-daily-table"] th').contains('盈亏').click()
    cy.get('[data-testid="reports-daily-table"]').should('contain', '2024-01-15')
  })
})
