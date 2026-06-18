describe('Review Risk History', () => {
  beforeEach(() => {
    cy.visitApp('/#/review')
    cy.contains('复盘工作台', { timeout: 10000 }).should('be.visible')
  })

  it('renders risk history panel on review page', () => {
    cy.get('[data-testid="review-risk-history"]').should('be.visible')
    cy.get('[data-testid="risk-history-panel"]').should('be.visible')
    cy.contains('风险历史').should('be.visible')
  })

  it('renders risk sparkline when history points exist', () => {
    cy.get('[data-testid="review-risk-history"]').should('be.visible')
    cy.get('[data-testid="risk-sparkline"]').should('exist')
  })
})
