describe('Watchlist score expiry', () => {
  it('updates stale badges and stale filtering after expires_at without reloading', () => {
    const now = Date.parse('2026-07-24T10:00:00Z')
    cy.clock(now, ['Date', 'setInterval', 'clearInterval'])
    cy.stubApi()
    cy.intercept('GET', '/api/watchlist/scores', {
      body: {
        scores: [
          {
            id: 101,
            symbol: 'NVDA.US',
            market: 'US',
            score: 72,
            rationale: '即将过期的量化评分',
            confidence: 0.8,
            recommended_action: 'CANDIDATE',
            source: 'quant_v1',
            created_at: '2026-07-24T09:00:00Z',
            expires_at: '2026-07-24T10:00:30Z',
            is_stale: false,
          },
        ],
        reviews: [],
      },
    }).as('getExpiringWatchlistScores')

    cy.visit('/#/watchlist')
    cy.wait('@getWatchlist')
    cy.wait('@getExpiringWatchlistScores')

    cy.get('[data-testid="watchlist-table"] tbody tr')
      .contains('tr', 'NVDA.US')
      .within(() => {
        cy.get('[data-testid="watchlist-stale-badge"]').should('not.exist')
      })

    cy.tick(60_000)

    cy.get('[data-testid="watchlist-table"] tbody tr')
      .contains('tr', 'NVDA.US')
      .within(() => {
        cy.get('[data-testid="watchlist-stale-badge"]')
          .should('contain.text', '已过期')
      })

    cy.get('[data-testid="watchlist-hide-stale"]').click()
    cy.get('[data-testid="watchlist-table"]')
      .should('not.contain', 'NVDA.US')
      .and('contain', 'AAPL.US')
  })
})
