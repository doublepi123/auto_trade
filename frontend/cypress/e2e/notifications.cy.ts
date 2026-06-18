describe('Notification Center', () => {
  beforeEach(() => {
    cy.visitApp('/#/notifications')
    cy.wait('@getNotifications')
  })

  it('lists dispatched notifications in card view by default', () => {
    cy.contains('通知中心', { timeout: 10000 }).should('be.visible')
    cy.get('[data-testid="notif-day-groups"]').should('be.visible')
    cy.get('[data-testid="notif-card-1"]').should('contain', '风控熔断')
    cy.get('[data-testid="notif-card-2"]').should('contain', '日报')
    cy.get('[data-testid="notif-card-3"]').should('contain', '发送失败')
  })

  it('toggles between card view and table view', () => {
    cy.get('[data-testid="notif-view-table"]').click()
    cy.get('[data-testid="notif-list"]').should('be.visible')
    cy.get('[data-testid="notif-day-groups"]').should('not.exist')

    cy.get('[data-testid="notif-view-cards"]').click()
    cy.get('[data-testid="notif-day-groups"]').should('be.visible')
    cy.get('[data-testid="notif-list"]').should('not.exist')
  })

  it('filters notifications by backend search text', () => {
    cy.get('input[placeholder="搜索标题/内容/错误"]').type('kill')
    cy.wait('@getNotifications')
    cy.get('[data-testid="notif-card-1"]').should('be.visible')
    cy.get('[data-testid="notif-card-2"]').should('not.exist')
    cy.contains('没有匹配的通知').should('not.exist')
  })

  it('shows empty state when backend search has no matches', () => {
    cy.get('input[placeholder="搜索标题/内容/错误"]').type('no-such-notification')
    cy.wait('@getNotifications')
    cy.contains('没有匹配的通知').should('be.visible')
    cy.get('[data-testid="notif-day-groups"]').should('not.exist')
  })

  it('quick filters by severity and failed status', () => {
    cy.get('[data-testid="notif-filter-critical"]').click()
    cy.wait('@getNotifications')
    cy.get('[data-testid="notif-card-1"]').should('be.visible')
    cy.get('[data-testid="notif-card-2"]').should('not.exist')
    cy.get('[data-testid="notif-card-3"]').should('not.exist')

    cy.get('[data-testid="notif-filter-failed"]').click()
    cy.wait('@getNotifications')
    cy.get('[data-testid="notif-card-3"]').should('be.visible')
    cy.get('[data-testid="notif-card-1"]').should('not.exist')
    cy.get('[data-testid="notif-card-2"]').should('not.exist')

    cy.get('[data-testid="notif-filter-all"]').click()
    cy.wait('@getNotifications')
    cy.get('[data-testid="notif-card-1"]').should('be.visible')
    cy.get('[data-testid="notif-card-2"]').should('be.visible')
    cy.get('[data-testid="notif-card-3"]').should('be.visible')
  })

  it('filters by success dropdown', () => {
    cy.get('[data-testid="notif-success"]').click()
    cy.get('.el-select-dropdown__item').contains('失败').click()
    cy.wait('@getNotifications')
    cy.get('[data-testid="notif-card-3"]').should('be.visible')
    cy.get('[data-testid="notif-card-1"]').should('not.exist')
    cy.get('[data-testid="notif-card-2"]').should('not.exist')
  })

  it('filters by date range', () => {
    cy.get('input[placeholder="开始日期"]').type('2026-06-15')
    cy.get('input[placeholder="结束日期"]').type('2026-06-15')
    // Trigger blur to close picker and apply
    cy.get('h3').click()
    cy.wait('@getNotifications')
    cy.get('[data-testid="notif-card-3"]').should('be.visible')
    cy.get('[data-testid="notif-card-1"]').should('not.exist')
    cy.get('[data-testid="notif-card-2"]').should('not.exist')
  })

  it('renders summary counts', () => {
    cy.get('[data-testid="notif-summary"]').should('contain', '成功 2')
    cy.get('[data-testid="notif-summary"]').should('contain', '失败 1')
    cy.get('[data-testid="notif-summary"]').should('contain', 'CRITICAL 1')
    cy.get('[data-testid="notif-summary"]').should('contain', 'INFO 1')
    cy.get('[data-testid="notif-summary"]').should('contain', 'WARNING 1')
  })

  it('combines severity dropdown with quick filters', () => {
    cy.get('[data-testid="notif-severity"]').click()
    cy.get('.el-select-dropdown__item').contains('CRITICAL').click()
    cy.wait('@getNotifications')
    cy.get('[data-testid="notif-card-1"]').should('be.visible')
    cy.get('[data-testid="notif-card-2"]').should('not.exist')
    cy.get('[data-testid="notif-card-3"]').should('not.exist')
  })

  it('switches to timeline view', () => {
    cy.get('[data-testid="notif-view-timeline"]').click()
    cy.get('[data-testid="notif-timeline"]').should('be.visible')
    cy.get('[data-testid="notif-timeline-item-1"]').should('be.visible')
    cy.get('[data-testid="notif-day-groups"]').should('not.exist')
    cy.get('[data-testid="notif-list"]').should('not.exist')
  })

  it('exports notifications as CSV', () => {
    cy.get('[data-testid="notif-export-csv"]').click()
    cy.wait('@exportNotifications')
    cy.get('@exportNotifications').its('request.query.format').should('equal', 'csv')
  })

  it('exports notifications as JSON', () => {
    cy.get('[data-testid="notif-export-json"]').click()
    cy.wait('@exportNotifications')
    cy.get('@exportNotifications').its('request.query.format').should('equal', 'json')
  })

  it('passes current filters to CSV export', () => {
    cy.get('[data-testid="notif-severity"]').click()
    cy.get('.el-select-dropdown__item').contains('CRITICAL').click()
    cy.wait('@getNotifications')

    cy.get('[data-testid="notif-export-csv"]').click()
    cy.wait('@exportNotifications')
    cy.get('@exportNotifications').its('request.query.severity').should('equal', 'CRITICAL')
    cy.get('@exportNotifications').its('request.query.format').should('equal', 'csv')
  })

  it('opens notification detail dialog from card view', () => {
    cy.get('[data-testid="notif-card-3"]').click()
    cy.get('[data-testid="notif-detail-dialog"]').should('be.visible')
    cy.contains('发送失败').should('be.visible')
    cy.contains('connection refused').should('be.visible')
  })

  it('opens notification detail dialog from table view', () => {
    cy.get('[data-testid="notif-view-table"]').click()
    cy.get('[data-testid="notif-list"] tbody tr').first().click()
    cy.get('[data-testid="notif-detail-dialog"]').should('be.visible')
  })

  it('shows copy error button for failed notification', () => {
    cy.get('[data-testid="notif-card-3"]').click()
    cy.get('[data-testid="notif-copy-error"]').should('be.visible')
  })
})
