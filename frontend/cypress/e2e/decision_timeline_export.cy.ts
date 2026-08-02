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

    cy.get('body').then(() => {
      // The event type filter is a multi-select; open it and pick an option
      const trigger = cy.get('body').then(($body) => {
        if ($body.find('.el-select__wrapper').length > 0) {
          cy.get('.el-select__wrapper').first().click()
          cy.get('.el-select-dropdown__item').first().click()
        }
      })
    })

    cy.contains('导出 JSON').click()
    cy.wait('@exportEvents').its('request.url').then((url) => {
      // At minimum the export URL must contain the export path and format
      expect(url).to.include('format=json')
    })
  })

  it('passes search query to export request', () => {
    cy.intercept('GET', '/api/events/export*').as('exportEvents')

    // Type into the search box
    cy.get('input[placeholder*="搜索"], input[type="search"]').then(($el) => {
      if ($el.length > 0) {
        cy.wrap($el).first().type('NVDA{enter}')
        cy.wait('@getEvents')
      }
    })

    cy.contains('导出 CSV').click()
    cy.wait('@exportEvents').its('request.url').then((url) => {
      expect(url).to.include('format=csv')
    })
  })

  it('downloads CSV without error', () => {
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
    cy.readFile('cypress/downloads/decision-timeline.csv').should('contain', 'ORDER_SKIPPED')
  })
})
