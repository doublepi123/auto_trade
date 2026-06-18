describe('Backtest CSV Export', () => {
  beforeEach(() => {
    cy.visitApp('/#/backtest')
    cy.wait('@getStrategy')
  })

  it('downloads a CSV when export button is clicked', () => {
    cy.intercept('POST', '/api/backtest/export', (req) => {
      req.reply({
        body: 'timestamp,action,price\n2026-05-22T10:00:00Z,BUY,100\n2026-05-22T10:01:00Z,SELL,200\n',
        headers: {
          'content-type': 'text/csv',
          'content-disposition': 'attachment; filename="backtest_export.csv"',
        },
      })
    }).as('exportBacktest')

    cy.get('[data-testid="run-backtest-button"]').click()
    cy.wait('@runBacktest')
    cy.get('[data-testid="backtest-trades"]').should('be.visible')
    cy.get('[data-testid="export-backtest-csv"]').should('be.visible').click()
    cy.wait('@exportBacktest')
      .its('request.body.result.metrics.total_pnl')
      .should('equal', 200)

    cy.readFile('cypress/downloads/backtest_export.csv').should('contain', 'BUY')
    cy.readFile('cypress/downloads/backtest_export.csv').should('contain', 'SELL')
  })
})
