describe('Market session awareness banner', () => {
  it('shows a dismissible banner when the market is outside RTH', () => {
    cy.stubApi()
    // Override the session route (later intercepts take precedence) so the
    // banner has a non-RTH phase to react to.
    cy.intercept('GET', '/api/calendar/session*', {
      body: {
        market: 'US',
        symbol: '',
        status: 'closed',
        is_trading: false,
        local_time: '2026-06-20 03:00:00 EST',
        utc_time: '2026-06-20T08:00:00Z',
        next_open: '2026-06-22T13:30:00Z',
      },
    }).as('sessionClosed')
    cy.visit('/')

    cy.get('[data-testid="session-banner"]', { timeout: 10000 })
      .should('be.visible')
      .and('contain', '休市')
      .and('contain', '非常规交易时段')

    // Dismiss hides it for the current phase.
    cy.get('[data-testid="session-banner"]').find('.el-alert__close-btn').click({ force: true })
    cy.get('[data-testid="session-banner"]').should('not.exist')
  })

  it('does not show the banner during regular trading hours', () => {
    cy.visitApp('/')
    cy.get('[data-testid="session-banner"]').should('not.exist')
  })
})
