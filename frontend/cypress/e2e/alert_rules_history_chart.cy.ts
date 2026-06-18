describe('Alert Rules History Chart', () => {
  beforeEach(() => {
    cy.visitApp('/#/alerts')
    cy.wait('@listAlertRules')
  })

  it('renders a sparkline for trigger values in the history dialog', () => {
    cy.intercept('GET', '/api/alert-rules*', {
      body: {
        items: [
          {
            id: 1,
            name: 'NVDA 高点',
            symbol: 'NVDA.US',
            rule_type: 'price_above',
            threshold: 175,
            severity: 'WARNING',
            enabled: true,
            cooldown_seconds: 300,
            last_fired_at: '2026-06-16T10:15:00Z',
            created_at: '2026-06-16T10:00:00Z',
          },
        ],
        total: 1,
      },
    }).as('listAlertRulesWithHistory')

    cy.contains('刷新').click()
    cy.wait('@listAlertRulesWithHistory')
    cy.get('[data-testid="alert-history"]').click()
    cy.wait('@getAlertRuleHistory')
    cy.get('[data-testid="alert-history-dialog"]').should('be.visible')
    cy.get('[data-testid="alert-history-chart"] svg').should('be.visible')
  })
})
