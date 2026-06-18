describe('Alert firing history', () => {
  it('opens a rule and shows its firing timeline', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules*', {
      body: {
        items: [
          {
            id: 7,
            name: 'AAPL 高点',
            symbol: 'AAPL.US',
            rule_type: 'price_above',
            threshold: 150,
            severity: 'WARNING',
            enabled: true,
            cooldown_seconds: 300,
            last_fired_at: '2026-06-16T03:00:00Z',
            created_at: '2026-06-15T00:00:00Z',
          },
        ],
        total: 1,
      },
    }).as('rules')
    cy.intercept('GET', '/api/alert-rules/7/history*', {
      body: {
        items: [
          {
            id: 2, rule_id: 7, symbol: 'AAPL.US', rule_type: 'price_above',
            threshold: 150, trigger_value: 160, severity: 'WARNING',
            message: 'AAPL.US 现价 160.00 ≥ 150.00', fired_at: '2026-06-16T03:00:00Z',
          },
          {
            id: 1, rule_id: 7, symbol: 'AAPL.US', rule_type: 'price_above',
            threshold: 150, trigger_value: 155, severity: 'WARNING',
            message: 'AAPL.US 现价 155.00 ≥ 150.00', fired_at: '2026-06-16T02:00:00Z',
          },
        ],
        total: 2,
      },
    }).as('history')

    cy.visit('/#/alerts')
    cy.wait('@rules')
    cy.get('[data-testid="alert-history"]').click()
    cy.wait('@history')

    cy.get('[data-testid="alert-history-dialog"]').should('be.visible')
    cy.contains('共 2 次触发').should('be.visible')
    cy.contains('160').should('be.visible')
    cy.contains('155').should('be.visible')
  })

  it('shows empty note when a rule has never fired', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules*', {
      body: {
        items: [
          {
            id: 9, name: 'never', symbol: 'TSLA.US', rule_type: 'price_below',
            threshold: 200, severity: 'WARNING', enabled: true, cooldown_seconds: 300,
            last_fired_at: null, created_at: '2026-06-15T00:00:00Z',
          },
        ],
        total: 1,
      },
    }).as('rules')
    cy.intercept('GET', '/api/alert-rules/9/history*', { body: { items: [], total: 0 } }).as('history')

    cy.visit('/#/alerts')
    cy.wait('@rules')
    cy.get('[data-testid="alert-history"]').click()
    cy.wait('@history')
    cy.contains('该规则尚未触发过').should('be.visible')
  })
})
