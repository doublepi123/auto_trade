describe('Watchlist triage helpers', () => {
  beforeEach(() => {
    cy.visitApp('/#/watchlist')
    cy.intercept('DELETE', '/api/watchlist/*', { body: { message: 'deleted' } }).as('deleteWatchlistItem')
    cy.wait('@getWatchlist')
    cy.wait('@getWatchlistQuotes')
    cy.wait('@getWatchlistScores')
  })

  it('supports client-side triage controls and bulk actions', () => {
    cy.get('[data-testid="watchlist-filter-summary"]').should('contain', '当前显示 2/2')
    cy.get('[data-testid="watchlist-last-refresh"]').should('contain', '行情最近成功刷新')

    cy.get('[data-testid="watchlist-search"]').type('Apple')
    cy.get('[data-testid="watchlist-table"]').should('contain', 'AAPL.US')
      .and('not.contain', 'NVDA.US')

    cy.get('[data-testid="watchlist-search"]').clear()
    cy.get('[data-testid="watchlist-market-filter"]').click()
    cy.contains('.el-select-dropdown__item', 'US').click()
    cy.get('[data-testid="watchlist-filter-summary"]').should('contain', '市场 US')

    cy.get('[data-testid="watchlist-status-filter"]').click()
    cy.contains('.el-select-dropdown__item', '交易中').click()
    cy.get('[data-testid="watchlist-filter-summary"]').should('contain', '状态 交易中')

    cy.get('[data-testid="watchlist-score-filter"]').click()
    cy.contains('.el-select-dropdown__item', '高分 ≥70').click()
    cy.get('[data-testid="watchlist-table"]').should('contain', 'NVDA.US')
      .and('not.contain', 'AAPL.US')

    cy.get('[data-testid="watchlist-hide-stale"]').click()
    cy.get('[data-testid="watchlist-filter-summary"]').should('contain', '隐藏过期评分')

    cy.get('[data-testid="watchlist-sort-mode"]').click()
    cy.contains('.el-select-dropdown__item', '评分从高到低').click()
    cy.get('[data-testid="watchlist-table"] tbody tr').first().should('contain', 'NVDA.US')

    cy.window().then((win) => {
      if (!win.navigator.clipboard) {
        Object.defineProperty(win.navigator, 'clipboard', { value: { writeText: () => undefined }, configurable: true })
      }
      cy.stub(win.navigator.clipboard, 'writeText').as('writeClipboard')
    })
    cy.get('[data-testid="watchlist-copy-symbol"]').first().click()
    cy.get('@writeClipboard').should('have.been.calledWith', 'NVDA.US')

    cy.get('[data-testid="watchlist-clear-filters"]').click()
    cy.get('[data-testid="watchlist-select-all"]').click()
    cy.get('[data-testid="watchlist-selection-summary"]').should('contain', '已选择 2')
    cy.get('[data-testid="watchlist-bulk-export"]').click()
    cy.document().its('body').should('contain', '已导出 2 个标的')

    cy.get('[data-testid="watchlist-bulk-delete"]').click()
    cy.get('[data-testid="watchlist-bulk-delete-confirm"]').click()
    cy.wait('@deleteWatchlistItem')
    cy.wait('@deleteWatchlistItem')
    cy.wait('@getWatchlist')

    cy.get('[data-testid="watchlist-refresh-now"]').click()
    cy.wait('@getWatchlistQuotes')
    cy.get('[data-testid="watchlist-last-refresh"]').should('contain', '行情最近成功刷新')

    cy.get('[data-testid="watchlist-search"]').type('NO_MATCH')
    cy.get('[data-testid="watchlist-filter-empty"]').should('contain', '没有匹配的观察标的')
  })
})
