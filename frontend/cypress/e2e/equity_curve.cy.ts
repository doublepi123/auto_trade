describe('Equity curve panel', () => {
  it('renders the cumulative realized PnL curve on the dashboard', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/equity/curve*', {
      body: {
        points: [
          { date: '2026-01-01', realized_pnl: 100, cumulative_pnl: 100, drawdown: 0, trade_count: 1 },
          { date: '2026-01-02', realized_pnl: -50, cumulative_pnl: 50, drawdown: 50, trade_count: 1 },
          { date: '2026-01-03', realized_pnl: -30, cumulative_pnl: 20, drawdown: 80, trade_count: 1 },
        ],
        total_realized_pnl: 20,
        max_drawdown: 80,
      },
    }).as('equity')

    cy.visit('/#/dashboard')
    cy.wait('@equity')

    cy.get('[data-testid="equity-curve-panel"]', { timeout: 10000 }).should('be.visible')
    cy.get('[data-testid="equity-curve-chart"]').should('exist')
    cy.contains('累计已实现（净）').should('be.visible')
    cy.contains('最大回撤').should('be.visible')
  })

  it('shows empty note when there are no closed trips', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/equity/curve*', {
      body: { points: [], total_realized_pnl: 0, max_drawdown: 0 },
    }).as('equity')
    cy.visit('/#/dashboard')
    cy.wait('@equity')
    cy.get('[data-testid="equity-curve-panel"]').should('be.visible')
    // No curve should render when there are fewer than 2 points.
    cy.get('[data-testid="equity-curve-chart"]').should('not.exist')
  })
})
