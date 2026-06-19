describe('Symbol Attribution Expectancy & Performance Tag', () => {
  beforeEach(() => {
    cy.visitApp('/')
    cy.wait('@getPnlBySymbol')
  })

  it('renders per-symbol expectancy and winner/loser tags', () => {
    cy.get('[data-testid="symbol-attribution-table"]').should('contain', 'AAPL.US')
    // AAPL expectancy = 300/6 = 50.00; NVDA expectancy = -50/2 = -25.00
    cy.get('[data-testid="attribution-expectancy"]').then(($els) => {
      const texts = $els.toArray().map((el) => el.textContent?.trim() ?? '')
      expect(texts).to.include('+$50.00')
      expect(texts).to.include('-$25.00')
    })
    cy.get('[data-testid="symbol-attribution-table"]').should('contain', '盈利')
    cy.get('[data-testid="symbol-attribution-table"]').should('contain', '亏损')
  })
})
