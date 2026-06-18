describe('Notification Center Retry', () => {
  beforeEach(() => {
    cy.visitApp('/#/notifications')
    cy.wait('@getNotifications')
  })

  it('retries a failed notification from the detail dialog', () => {
    cy.intercept('POST', '/api/notifications/3/retry', {
      body: {
        id: 3,
        title: '发送失败',
        content: 'webhook timeout',
        severity: 'WARNING',
        success: true,
        error: '',
        created_at: '2026-06-15T10:00:10Z',
      },
    }).as('retryNotification')

    cy.get('[data-testid="notif-card-3"]').click()
    cy.get('[data-testid="notif-detail-dialog"]').should('be.visible')
    cy.contains('失败').should('be.visible')
    cy.get('[data-testid="notif-detail-dialog"]').find('[data-testid="notif-retry-btn"]').should('be.visible').click()
    cy.wait('@retryNotification')
    cy.contains('成功').should('be.visible')
  })
})
