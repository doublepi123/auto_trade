describe('Alert Rules', () => {
  it('creates a rule and runs evaluation', () => {
    cy.visitApp('/#/alerts')
    cy.contains('告警规则', { timeout: 10000 }).should('be.visible')

    cy.get('[data-testid="alert-create"]').click()
    cy.get('[data-testid="alert-dialog"]').should('be.visible')
    cy.get('input[placeholder="规则名称"]').type('NVDA 高点')
    cy.get('[data-testid="alert-save"]').click()
    cy.wait('@createAlertRule')

    cy.get('[data-testid="alert-evaluate"]').click()
    cy.wait('@evaluateAlertRules')
    cy.contains('评估完成').should('be.visible')
  })
})
