describe('Position P&L Derived Stats & CSV Export', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.intercept('GET', '/api/positions/pnl', {
      body: {
        positions: [
          { symbol: 'AAPL.US', quantity: 100, avg_entry_cost: 150, last_price: 160, unrealized_pnl: 1000, unrealized_pnl_pct: 6.67, market_value: 16000, cost_value: 15000, has_quote: true },
          { symbol: 'NVDA.US', quantity: 50, avg_entry_cost: 200, last_price: 190, unrealized_pnl: -500, unrealized_pnl_pct: -5, market_value: 9500, cost_value: 10000, has_quote: true },
        ],
        total_unrealized_pnl: 500,
        total_cost_basis: 25000,
        total_unrealized_pnl_pct: 2,
        available: true,
        error: null,
      },
    }).as('getPositionPnl')
    cy.visit('/')
    cy.wait('@getPositionPnl')
  })

  it('renders derived winner/loser/concentration stats', () => {
    cy.get('[data-testid="position-pnl-derived"]').should('contain', '盈利 1')
    cy.get('[data-testid="position-pnl-derived"]').should('contain', '亏损 1')
    cy.get('[data-testid="position-pnl-derived"]').should('contain', '最大贡献 AAPL.US')
    cy.get('[data-testid="position-pnl-derived"]').should('contain', '集中度 AAPL.US 60%')
  })

  it('exports positions as CSV', () => {
    cy.get('[data-testid="position-pnl-export"]').should('not.be.disabled').click()
    cy.document().its('body').should('contain', '已导出 2 个持仓')
  })
})
