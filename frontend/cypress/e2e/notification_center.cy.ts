describe('Notification Center (P79-P83 Observability)', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.intercept('GET', '/api/notifications?*', (req) => {
      let items = [
        { id: 1, severity: 'CRITICAL', success: true, title: '风控熔断', content: 'kill switch triggered', error: '', created_at: '2026-06-17T10:00:00Z' },
        { id: 2, severity: 'WARNING', success: false, title: 'Webhook失败', content: 'AAPL.US alert', error: 'timeout', created_at: '2026-06-17T09:00:00Z' },
        { id: 3, severity: 'INFO', success: true, title: '日报', content: 'AAPL.US +200', error: '', created_at: '2026-06-16T22:00:00Z' },
        { id: 4, severity: 'WARNING', success: true, title: '价格提醒', content: 'TSLA.US touched range', error: '', created_at: '2026-06-16T21:00:00Z' },
      ]
      const params = req.query
      if (params.severity) {
        items = items.filter((i) => i.severity === params.severity)
      }
      if (params.success !== undefined) {
        items = items.filter((i) => String(i.success) === params.success)
      }
      if (params.q) {
        const q = String(params.q).toLowerCase()
        items = items.filter((i) =>
          i.title.toLowerCase().includes(q) ||
          i.content.toLowerCase().includes(q) ||
          i.error.toLowerCase().includes(q)
        )
      }
      req.reply({ body: { items, total: items.length, page: 1, page_size: 20 } })
    }).as('getNotifications')
    cy.visit('/#/notifications')
    cy.wait('@getNotifications')
  })

  it('shows notification observability summary and filters', () => {
    cy.wait(500)
    cy.get('[data-testid="notif-summary"]').should('contain', '4')
      .and('contain', '3')
      .and('contain', '1')
      .and('contain', 'CRITICAL')
      .and('contain', 'WARNING')
      .and('contain', 'INFO')
      .and('contain', '当前页')

    cy.get('[data-testid="notif-day-groups"]').should('contain', '风控熔断')
      .and('contain', 'Webhook失败')
      .and('contain', '日报')
      .and('contain', '价格提醒')

    cy.get('[data-testid="notif-view-table"]').click()
    cy.get('[data-testid="notif-filter-failed"]').click()
    cy.get('[data-testid="notif-list"]').should('contain', 'Webhook失败')
      .and('not.contain', '日报')

    cy.get('[data-testid="notif-search"]').type('TSLA')
    cy.get('[data-testid="notif-list"]').should('not.contain', 'TSLA')
      .and('not.contain', '价格提醒')
    cy.contains('没有匹配的通知').should('be.visible')

    cy.get('[data-testid="notif-filter-all"]').click()
    cy.get('[data-testid="notif-list"]').should('contain', 'TSLA')
      .and('contain', '价格提醒')

    cy.get('[data-testid="notif-search"]').clear()
    cy.get('[data-testid="notif-severity"]').click()
    cy.contains('.el-select-dropdown__item', 'WARNING').click()
    cy.wait('@getNotifications')
    cy.contains('button', 'CRITICAL').click()
    cy.get('[data-testid="notif-list"]').should('contain', '风控熔断')
      .and('not.contain', '日报')
  })
})
