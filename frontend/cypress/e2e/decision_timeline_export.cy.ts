describe('Decision Timeline Export Filters', () => {
  beforeEach(() => {
    cy.visitApp('/#/events')
    cy.wait('@getEvents')
  })

  it('passes active source filter to export request', () => {
    cy.intercept('GET', '/api/events/export*').as('exportEvents')

    // Select the audit source filter
    cy.get('[data-testid="timeline-source-filter"]').contains('审计').click()
    cy.wait('@getEvents')

    cy.contains('导出 CSV').click()
    cy.wait('@exportEvents').its('request.url').then((url) => {
      expect(url).to.include('source=audit')
    })
  })

  it('passes active event type filter to export request', () => {
    cy.intercept('GET', '/api/events/export*').as('exportEvents')

    cy.get('[data-testid="event-type-filter"] .el-select__wrapper').click()
    cy.get('.el-select-dropdown:visible').contains('.el-select-dropdown__item', 'ORDER_FILLED').click()
    cy.get('.el-select-dropdown:visible').contains('.el-select-dropdown__item', 'ORDER_SKIPPED').click()
    cy.get('h3').click()
    cy.wait('@getEvents')

    cy.contains('导出 JSON').click()
    cy.wait('@exportEvents').its('request.url').then((url) => {
      const params = new URL(url).searchParams
      expect(params.get('format')).to.equal('json')
      expect(params.getAll('event_type')).to.deep.equal(['ORDER_FILLED', 'ORDER_SKIPPED'])
    })
  })

  it('passes search query to export request', () => {
    cy.intercept('GET', '/api/events/export*').as('exportEvents')

    cy.get('[data-testid="timeline-search"]').then(($search) => {
      const input = $search.is('input') ? $search : $search.find('input')
      expect(input).to.have.length(1)
      cy.wrap(input).type('NVDA{enter}')
    })
    cy.wait('@getEvents')

    cy.contains('导出 CSV').click()
    cy.wait('@exportEvents').its('request.url').then((url) => {
      const params = new URL(url).searchParams
      expect(params.get('format')).to.equal('csv')
      expect(params.get('q')).to.equal('NVDA')
    })
  })

  it('passes active skip category to export request', () => {
    cy.intercept('GET', '/api/events/export*').as('exportEvents')

    cy.get('[data-testid="skip-category-filter"] .el-select__wrapper').click()
    cy.get('.el-select-dropdown:visible').contains('.el-select-dropdown__item', '风控阻断').click()
    cy.wait('@getEvents')

    cy.contains('导出 CSV').click()
    cy.wait('@exportEvents').its('request.url').then((url) => {
      expect(new URL(url).searchParams.get('skip_category')).to.equal('RISK')
    })
  })

  it('downloads CSV without error', () => {
    cy.exec('rm -f "cypress/downloads/decision-timeline.csv"')
    cy.intercept('GET', '/api/events/export*', (req) => {
      req.reply({
        body: 'source,id,event_type,symbol,created_at\ntrade,1,ORDER_SKIPPED,AAPL.US,2026-06-14T10:00:00Z\n',
        headers: {
          'content-type': 'text/csv',
          'content-disposition': 'attachment; filename="decision-timeline.csv"',
        },
      })
    }).as('exportEvents')

    cy.contains('导出 CSV').click()
    cy.wait('@exportEvents')
    cy.readFile('cypress/downloads/decision-timeline.csv', 'utf8').should('contain', 'ORDER_SKIPPED')
  })
})
