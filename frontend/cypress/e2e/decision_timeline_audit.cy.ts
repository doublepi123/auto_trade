describe('Decision Timeline audit source filter', () => {
  it('shows audit rows for source audit / all', () => {
    const auditItem = {
      id: 101,
      source: 'audit',
      event_type: 'KILL_SWITCH',
      symbol: '',
      broker_order_id: '',
      side: '',
      status: '',
      message: '{"reason":"test"}',
      payload: { reason: 'test' },
      actor_hash: 'abc12345deadbeef',
      source_ip: '127.0.0.1',
      severity: 'CRITICAL',
      result: 'SUCCESS',
      created_at: '2026-05-26T01:00:00.000Z',
    }

    cy.stubApi()
    cy.intercept('GET', '/api/events*', (req) => {
      const src = req.query.source
      if (src === 'trade') {
        req.reply({ body: { items: [], total: 0, page: 1, page_size: 20 } })
        return
      }
      req.reply({
        body: {
          items: [auditItem],
          total: 1,
          page: 1,
          page_size: 20,
        },
      })
    }).as('getEventsAudit')

    cy.visit('/#/events')
    cy.get('h3', { timeout: 10000 }).should('contain', '决策时间线')
    cy.wait('@getEventsAudit')
    cy.contains('紧急停止').should('be.visible')

    cy.get('[data-testid="timeline-source-filter"]')
      .find('input[value="audit"]')
      .click({ force: true })
    cy.wait('@getEventsAudit')
    cy.contains('紧急停止').should('be.visible')

    cy.get('[data-testid="timeline-source-filter"]')
      .find('input[value="all"]')
      .click({ force: true })
    cy.wait('@getEventsAudit')
    cy.contains('紧急停止').should('be.visible')
  })
})
