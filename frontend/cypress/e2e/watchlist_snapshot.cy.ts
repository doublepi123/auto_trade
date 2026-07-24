describe('Watchlist Snapshot & Derived Columns', () => {
  beforeEach(() => {
    cy.visitApp('/#/watchlist')
    cy.wait('@getWatchlist')
    cy.wait('@getWatchlistQuotes')
    cy.wait('@getWatchlistScores')
  })

  it('renders spread column from ask-bid and surfaces stale score badge', () => {
    cy.get('[data-testid="watchlist-spread"]').should('have.length.at.least', 1)
    // NVDA.US: ask 180.6 - bid 180.4 = 0.20
    cy.get('[data-testid="watchlist-spread"]').first().should('contain', '0.20')
    cy.get('[data-testid="watchlist-stale-badge"]').should('exist')
    cy.get('.score-source').should('contain.text', '量化 v3')
  })

  it('export snapshot button is enabled and triggers a success toast', () => {
    cy.get('[data-testid="watchlist-export-csv"]').should('not.be.disabled')
    cy.get('[data-testid="watchlist-export-csv"]').click()
    cy.document().its('body').should('contain', '已导出观察列表快照')
  })
})
