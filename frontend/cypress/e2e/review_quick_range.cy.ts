describe('Review Quick Date Ranges', () => {
  beforeEach(() => {
    cy.visitApp('/#/review')
    cy.wait('@getLLMIntervalStatus')
  })

  it('fills date range and searches with quick 7-day button', () => {
    cy.get('[data-testid="review-quick-7d"]').click()
    cy.wait('@getReview')
    cy.get('input[placeholder="例如 AAPL.US"]').should('have.value', 'AAPL.US')
    cy.get('input').then(($inputs) => {
      const dates = $inputs
        .filter('[placeholder]')
        .map((_, el) => (el as HTMLInputElement).value)
        .get()
        .filter((v) => v && v.match(/^\d{4}-\d{2}-\d{2}$/))
      expect(dates.length).to.equal(2)
    })
    cy.get('[data-testid="review-runtime-history"]').should('be.visible')
  })

  it('fills date range and searches with quick 30-day button', () => {
    cy.get('[data-testid="review-quick-30d"]').click()
    cy.wait('@getReview')
    cy.get('[data-testid="review-runtime-history"]').should('be.visible')
  })

  it('fills date range and searches with quick 90-day button', () => {
    cy.get('[data-testid="review-quick-90d"]').click()
    cy.wait('@getReview')
    cy.get('[data-testid="review-runtime-history"]').should('be.visible')
  })
})
