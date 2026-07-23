describe('Dashboard Diagnostics CSV Export', () => {
  beforeEach(() => {
    cy.visitApp('/')
    cy.wait('@getDiagnostics')
  })

  it('exports the diagnostics snapshot as CSV', () => {
    cy.get('[data-testid="dashboard-diagnostics"]').should('contain', '运行时总数')
    cy.get('[data-testid="live-regime-gate-mode"]').should('contain.text', '启用')
    cy.get('[data-testid="live-regime-max-age"]').should('contain.text', '900.0s')
    cy.get('[data-testid="live-max-entries-per-symbol"]').should('contain.text', '2 次')
    cy.get('[data-testid="dash-diagnostics-export"]').should('not.be.disabled').click()
    cy.document().its('body').should('contain', '已导出')
  })
})
