describe('Decision Timeline Bookmarks JSON Import/Export', () => {
  beforeEach(() => {
    cy.stubApi()
    // Seed one bookmark via localStorage before visit so export has content.
    cy.window().then((win) => {
      win.localStorage.setItem('auto_trade.timeline.bookmarks.v1', JSON.stringify([
        { id: 'bm_seed', label: '种子书签', source: 'all', event_types: ['ORDER_FILLED'], skip_category: '', q: 'AAPL', created_at: 1718000000000 },
      ]))
    })
    cy.visit('/#/events')
    cy.wait('@getEvents')
  })

  it('exports the seeded bookmarks as JSON', () => {
    cy.get('[data-testid="timeline-bookmarks-export"]').click()
    cy.document().its('body').should('contain', '已导出 1 个书签')
  })

  it('imports bookmarks from a JSON file and merges into the list', () => {
    const incoming = [
      { id: 'bm_new', label: '导入的书签', source: 'llm', event_types: [], skip_category: '', q: 'NVDA', created_at: 1718000000001 },
    ]
    cy.writeFile('cypress/fixtures/temp-bookmarks.json', JSON.stringify(incoming))
    cy.get('[data-testid="timeline-bookmarks-input"]').selectFile('cypress/fixtures/temp-bookmarks.json', { force: true })
    cy.document().its('body').should('contain', '新增 1')
    cy.document().its('body').should('contain', '导入的书签')
  })
})
