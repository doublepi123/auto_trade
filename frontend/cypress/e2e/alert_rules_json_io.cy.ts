describe('Alert Rules JSON Import/Export', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules*', {
      body: {
        items: [
          { id: 1, name: 'NVDA above 175', symbol: 'NVDA.US', rule_type: 'price_above', threshold: 175, severity: 'WARNING', enabled: true, cooldown_seconds: 300, last_fired_at: null, created_at: '2026-06-16T12:00:00Z' },
        ],
        total: 1,
      },
    }).as('listAlertRules')
    cy.visit('/#/alerts')
    cy.wait('@listAlertRules')
  })

  it('exports the loaded rules as JSON', () => {
    cy.get('[data-testid="alert-export-json"]').should('not.be.disabled').click()
    cy.document().its('body').should('contain', '已导出 1 条规则')
  })

  it('imports rules from a JSON file via bulk create', () => {
    const rules = [
      { name: 'AAPL above 200', symbol: 'AAPL.US', rule_type: 'price_above', threshold: 200, severity: 'WARNING', enabled: true, cooldown_seconds: 120 },
      { name: 'Daily loss -500', symbol: '', rule_type: 'daily_loss', threshold: -500, severity: 'CRITICAL', enabled: true, cooldown_seconds: 600 },
    ]
    cy.writeFile('cypress/fixtures/temp-alert-rules.json', JSON.stringify(rules))
    cy.get('[data-testid="alert-import-input"]').selectFile('cypress/fixtures/temp-alert-rules.json', { force: true })
    // Two create calls happen sequentially
    cy.wait('@createAlertRule')
    cy.wait('@createAlertRule')
    cy.document().its('body').should('contain', '已导入 2 条规则')
  })
})
