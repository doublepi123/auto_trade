describe('Symbol attribution panel', () => {
  it('renders per-symbol realized PnL breakdown on the dashboard', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/pnl/by-symbol*', {
      body: {
        rows: [
          { symbol: 'TSLA.US', realized_pnl: 200, trade_count: 2, win_count: 2, win_rate: 100, contribution_share: 1.5385, largest_win: 120, largest_loss: 80 },
          { symbol: 'MSFT.US', realized_pnl: -120, trade_count: 1, win_count: 0, win_rate: 0, contribution_share: -0.9231, largest_win: -120, largest_loss: -120 },
        ],
        total_realized_pnl: 130,
      },
    }).as('attr')

    cy.visit('/#/dashboard')
    cy.wait('@attr')

    cy.get('[data-testid="symbol-attribution-panel"]', { timeout: 10000 }).should('be.visible')
    cy.get('[data-testid="symbol-attribution-table"]').should('exist')
    cy.contains('TSLA.US').should('be.visible')
    cy.contains('MSFT.US').should('be.visible')
  })

  it('shows empty note when no realized trades exist', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/pnl/by-symbol*', { body: { rows: [], total_realized_pnl: 0 } }).as('attr')
    cy.visit('/#/dashboard')
    cy.wait('@attr')
    cy.get('[data-testid="symbol-attribution-panel"]').should('be.visible')
    cy.contains('暂无已实现成交').should('be.visible')
  })
})
