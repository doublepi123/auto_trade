describe('Equity Curve Derived Stats', () => {
  beforeEach(() => {
    cy.visitApp('/')
    cy.wait('@getEquityCurve')
  })

  it('renders peak/trough/period-return derived from loaded points', () => {
    cy.get('[data-testid="equity-derived"]').should('contain', '峰值 +260.00')
    cy.get('[data-testid="equity-derived"]').should('contain', '谷值 0.00')
    cy.get('[data-testid="equity-derived"]').should('contain', '区间回报 +$260.00')
    // best day delta = 200 (06-13), worst = -60 (06-12)
    cy.get('[data-testid="equity-derived"]').should('contain', '最佳日 +200.00')
    cy.get('[data-testid="equity-derived"]').should('contain', '最差日 -60.00')
  })
})
