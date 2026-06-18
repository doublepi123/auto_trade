describe('Watchlist Score Detail', () => {
  beforeEach(() => {
    cy.visitApp('/#/watchlist')
    cy.wait('@getWatchlist')
    cy.wait('@getWatchlistScores')
  })

  it('opens score detail drawer when clicking a score tag', () => {
    cy.get('[data-testid="watchlist-score-tag"]').first().click()
    cy.get('[data-testid="watchlist-score-drawer"]').should('be.visible')
    cy.contains('LLM 评分详情').should('be.visible')
    cy.contains('价格处于布林带中轨上方').should('be.visible')
  })

  it('displays score rationale, confidence, source and timestamps', () => {
    cy.get('[data-testid="watchlist-score-tag"]').first().click()
    cy.contains('评分依据').should('be.visible')
    cy.contains('85%').should('be.visible')
    cy.contains('llm').should('be.visible')
    cy.contains('生成：').should('be.visible')
    cy.contains('过期：').should('be.visible')
  })

  it('closes the drawer', () => {
    cy.get('[data-testid="watchlist-score-tag"]').first().click()
    cy.get('[data-testid="watchlist-score-drawer"]').should('be.visible')
    cy.get('.el-drawer__close-btn').click()
    cy.get('[data-testid="watchlist-score-drawer"]').should('not.be.visible')
  })
})
