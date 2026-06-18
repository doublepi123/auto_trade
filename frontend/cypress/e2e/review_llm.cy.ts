describe('Review LLM Status Card', () => {
  beforeEach(() => {
    cy.visitApp('/#/review')
    cy.wait('@getLLMIntervalStatus')
  })

  it('renders the LLM status card on the review page', () => {
    cy.get('[data-testid="review-llm-status-card"]').should('be.visible')
    cy.contains('LLM 实时状态').should('be.visible')
    cy.get('[data-testid="review-llm-status-card"]').contains('已启用').should('be.visible')
  })

  it('shows LLM budget counters for the current symbol', () => {
    cy.get('[data-testid="review-llm-budget-bar"]').should('be.visible')
    cy.get('[data-testid="review-llm-budget-tracked"]').should('contain', '2/5')
    cy.get('[data-testid="review-llm-budget-hourly"]').should('contain', '12/60')
    cy.get('[data-testid="review-llm-budget-remaining"]').should('contain', '48')
  })

  it('shows selected symbol schedule status', () => {
    cy.get('[data-testid="review-selected-symbol-llm-status"]').should('be.visible')
    cy.get('[data-testid="review-selected-symbol-llm-status"]').should('contain', 'AAPL.US')
    cy.get('[data-testid="review-selected-symbol-llm-status"]').contains('主标的').should('be.visible')
    cy.get('[data-testid="review-selected-symbol-llm-status"]').contains('45s').should('be.visible')
    cy.get('[data-testid="review-selected-symbol-llm-status"]').should('contain', '同方向冷却中')
  })

  it('updates selected symbol status when the form symbol changes', () => {
    cy.get('[data-testid="review-selected-symbol-llm-status"]').should('contain', 'AAPL.US')
    cy.get('input[placeholder="例如 AAPL.US"]').clear().type('NVDA.US')
    cy.get('button').contains('查询').click()
    cy.get('[data-testid="review-selected-symbol-llm-status"]').should('contain', 'NVDA.US')
    cy.get('[data-testid="review-selected-symbol-llm-status"]').contains('观察标的').should('be.visible')
    cy.get('[data-testid="review-selected-symbol-llm-status"]').contains('120s').should('be.visible')
  })
})
