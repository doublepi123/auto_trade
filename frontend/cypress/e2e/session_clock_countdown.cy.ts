describe('Session Clock Next-Open Countdown', () => {
  it('shows a live HH:MM:SS countdown when the market is closed', () => {
    const future = new Date(Date.now() + 2 * 3600 * 1000).toISOString()
    cy.stubApi()
    cy.intercept('GET', '/api/calendar/session*', {
      body: {
        market: 'US',
        symbol: 'AAPL.US',
        status: 'closed',
        is_trading: false,
        local_time: '2026-06-19 09:00:00 EDT',
        utc_time: '2026-06-19T13:00:00Z',
        next_open: future,
      },
    }).as('getMarketSession')
    cy.visit('/')
    cy.wait('@getMarketSession')
    cy.get('[data-testid="session-countdown"]').should('be.visible')
    cy.get('[data-testid="session-countdown"]').should('contain', '距开盘')
    cy.get('[data-testid="session-countdown"]').invoke('text').should('match', /\d{2}:\d{2}:\d{2}/)
  })

  it('hides the countdown while the market is open', () => {
    cy.visitApp('/')
    cy.wait('@getMarketSession')
    cy.get('[data-testid="session-status"]').should('contain', 'RTH')
    cy.get('[data-testid="session-countdown"]').should('not.exist')
  })
})
