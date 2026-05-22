describe('Dashboard Charts', () => {
  it('loads price and pnl charts with trade signal markers', () => {
    cy.visitApp('/')
    cy.get('[data-testid="price-chart"]', { timeout: 10000 }).should('be.visible')
    cy.get('[data-testid="pnl-chart"]').should('be.visible')
    cy.get('[data-testid="trade-signal-marker"]').should('exist')
    cy.contains('价格走势').should('be.visible')
    cy.contains('盈亏曲线').should('be.visible')
    cy.contains('交易信号').should('be.visible')
  })
})
