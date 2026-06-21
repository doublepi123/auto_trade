describe('Command palette', () => {
  beforeEach(() => {
    cy.visitApp('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
  })

  it('opens from the header button and lists commands', () => {
    cy.get('[data-testid="nav-command-palette"]').click()
    cy.get('[data-testid="command-palette"]').should('be.visible')
    cy.get('[data-testid="command-list"] .command-item').should('have.length.greaterThan', 0)
  })

  it('filters commands by query', () => {
    cy.get('[data-testid="nav-command-palette"]').click()
    cy.get('[data-testid="command-palette-input"]').type('观察')
    cy.get('[data-testid="command-list"]').should('contain', '观察列表')
  })

  it('runs a navigation command and closes', () => {
    cy.get('[data-testid="nav-command-palette"]').click()
    cy.get('[data-testid="command-palette-input"]').type('告警{enter}')
    cy.get('[data-testid="command-palette"]').should('not.exist')
    cy.contains('告警规则', { timeout: 10000 }).should('be.visible')
  })

  it('offers per-symbol chart jumps', () => {
    cy.get('[data-testid="nav-command-palette"]').click()
    cy.get('[data-testid="command-palette-input"]').type('AAPL')
    cy.get('[data-testid="command-list"]').should('contain', '在仪表盘查看 AAPL.US')
    cy.get('[data-testid="command-palette-input"]').type('{enter}')
    // Default chart symbol is the first option (NVDA.US); jumping to AAPL.US
    // proves the request was honoured.
    cy.get('[data-testid="chart-symbol-current"]', { timeout: 10000 }).should(
      'contain',
      'AAPL.US',
    )
  })
})
