describe('Notification Center Severity Distribution', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.intercept('GET', '/api/notifications?*', {
      body: {
        items: [
          { id: 1, title: '熔断', content: 'x', severity: 'CRITICAL', success: true, error: '', created_at: '2026-06-17T10:00:00Z' },
          { id: 2, title: '熔断2', content: 'x', severity: 'CRITICAL', success: true, error: '', created_at: '2026-06-17T10:01:00Z' },
          { id: 3, title: '告警', content: 'x', severity: 'WARNING', success: false, error: 'timeout', created_at: '2026-06-17T09:00:00Z' },
          { id: 4, title: '日报', content: 'x', severity: 'INFO', success: true, error: '', created_at: '2026-06-16T22:00:00Z' },
        ],
        total: 4,
        page: 1,
        page_size: 50,
      },
    }).as('getNotifications')
    cy.visit('/#/notifications')
    cy.wait('@getNotifications')
  })

  it('renders severity distribution bars derived from loaded items', () => {
    cy.get('[data-testid="notif-distribution"]').should('be.visible')
    cy.get('[data-testid="dist-bar-critical"]').should('be.visible')
    cy.get('[data-testid="notif-distribution"]').should('contain', 'CRITICAL 2')
    cy.get('[data-testid="notif-distribution"]').should('contain', 'WARNING 1')
    cy.get('[data-testid="notif-distribution"]').should('contain', 'INFO 1')
  })

  it('renders success/failure ratio from loaded items', () => {
    cy.get('[data-testid="notif-distribution"]').should('contain', '成功率 75%')
    cy.get('[data-testid="notif-distribution"]').should('contain', '3') // 3 successes
    cy.get('[data-testid="notif-distribution"]').should('contain', '1') // 1 failure
  })
})
