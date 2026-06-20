describe('Notification Center (P79-P83 Observability)', () => {
  let requestedPageSizes: string[] = []

  beforeEach(() => {
    requestedPageSizes = []
    cy.stubApi()
    cy.intercept('GET', '/api/notifications?*', (req) => {
      let items = [
        { id: 1, severity: 'CRITICAL', success: true, title: '风控熔断', content: 'kill switch triggered', error: '', created_at: '2026-06-17T10:00:00Z' },
        { id: 2, severity: 'WARNING', success: false, title: 'Webhook失败', content: 'AAPL.US alert', error: 'timeout', created_at: '2026-06-17T09:00:00Z' },
        { id: 3, severity: 'INFO', success: true, title: '日报', content: 'AAPL.US +200', error: '', created_at: '2026-06-16T22:00:00Z' },
        { id: 4, severity: 'WARNING', success: true, title: '价格提醒', content: 'TSLA.US touched range', error: '', created_at: '2026-06-16T21:00:00Z' },
      ]
      const params = req.query
      requestedPageSizes.push(String(params.page_size || ''))
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

  it('supports page-level triage helpers', () => {
    cy.get('[data-testid="notif-success-rate"]').should('contain', '成功率 75%')
    cy.get('[data-testid="notif-time-span"]').should('contain', '时间 ')
      .and('contain', ' - ')

    cy.get('[data-testid="notif-view-table"]').click()
    cy.get('[data-testid="notif-filter-failed"]').click()
    cy.get('[data-testid="notif-active-filters"]').should('contain', '失败')
    cy.get('[data-testid="notif-failure-note"]').should('contain', '当前仅查看失败通知')

    cy.get('[data-testid="notif-reset-filters"]').click()
    cy.get('[data-testid="notif-active-filters"]').should('contain', '无筛选')
    cy.get('[data-testid="notif-list"]').should('contain', '风控熔断')
      .and('contain', '价格提醒')

    cy.get('[data-testid="notif-symbol-filter"]').click()
    cy.contains('.el-select-dropdown__item', 'TSLA.US').click()
    cy.get('[data-testid="notif-list"]').should('contain', '价格提醒')
      .and('not.contain', 'AAPL.US alert')
    cy.get('[data-testid="notif-active-filters"]').should('contain', '标的 TSLA.US')

    cy.get('[data-testid="notif-page-size"]').click()
    cy.contains('.el-select-dropdown__item', '10 / 页').click()
    cy.wrap(null).should(() => {
      expect(requestedPageSizes).to.include('10')
    })
    cy.get('[data-testid="notif-page-size-note"]').should('contain', '每页 10 条')

    cy.get('[data-testid="notif-reset-filters"]').click()
    cy.get('[data-testid="notif-sort-order"]').click()
    cy.contains('.el-select-dropdown__item', '最早优先').click()
    cy.get('[data-testid="notif-list"] tbody tr').first().should('contain', '价格提醒')
    cy.get('[data-testid="notif-sort-order"]').click()
    cy.contains('.el-select-dropdown__item', '最新优先').click()
    cy.get('[data-testid="notif-list"] tbody tr').first().should('contain', '风控熔断')

    cy.get('[data-testid="notif-view-cards"]').click()
    cy.get('[data-testid="notif-card-1"]').click({ force: true })
    cy.get('[data-testid="notif-detail-meta-extra"]').should('contain', '#1')
      .and('contain', '风控熔断')
    cy.window().then((win) => {
      cy.stub(win.navigator.clipboard, 'writeText').as('writeClipboard')
    })
    cy.get('[data-testid="notif-copy-content"]').click()
    cy.get('@writeClipboard').should('have.been.calledWith', 'kill switch triggered')
  })

  it('supports second-wave triage helpers', () => {
    cy.window().then((win) => {
      cy.stub(win.navigator.clipboard, 'writeText').as('writeClipboard')
    })

    cy.get('[data-testid="notif-error-rate"]').should('contain', '失败率 25%')

    cy.get('[data-testid="notif-unread-only"]').click()
    cy.get('[data-testid="notif-active-filters"]').should('contain', '仅未读')
    cy.get('[data-testid="notif-day-groups"]').should('contain', '风控熔断')

    cy.get('[data-testid="notif-copy-page"]').click()
    cy.get('@writeClipboard').should('have.been.calledWithMatch', '风控熔断')
      .and('have.been.calledWithMatch', 'Webhook失败')

    cy.get('[data-testid="notif-category-filter"]').click()
    cy.contains('.el-select-dropdown__item', 'Webhook').click()
    cy.get('[data-testid="notif-active-filters"]').should('contain', '推断类别 Webhook')
    cy.get('[data-testid="notif-day-groups"]').should('contain', 'Webhook失败')
      .and('not.contain', '日报')

    cy.get('[data-testid="notif-category-filter"]').click()
    cy.contains('.el-select-dropdown__item', '全部类别').click()
    cy.get('[data-testid="notif-group-result"]').click()
    cy.get('[data-testid="notif-result-groups"]').should('contain', '成功通知')
      .and('contain', '失败通知')
      .and('contain', '重试发送')

    cy.get('[data-testid="notif-quick-recent-days"]').click()
    cy.get('[data-testid="notif-active-filters"]').should('contain', '最近日期范围')
    cy.get('@getNotifications.all').should('have.length.greaterThan', 1)

    cy.get('[data-testid="notif-search"]').clear().type('Webhook')
    cy.get('[data-testid="notif-highlight"]').should('contain', 'Webhook')
    cy.get('[data-testid="notif-highlight-match"]').should('contain', 'Webhook')

    cy.get('[data-testid="notif-view-table"]').click()
    cy.get('[data-testid="notif-page-size"]').click()
    cy.contains('.el-select-dropdown__item', '20 / 页').click()
    cy.reload()
    cy.wait('@getNotifications')
    cy.window().then((win) => {
      cy.stub(win.navigator.clipboard, 'writeText').as('writeClipboardAfterReload')
    })
    cy.get('[data-testid="notif-list"]').should('be.visible')
    cy.get('[data-testid="notif-page-size-note"]').should('contain', '每页 20 条')

    cy.get('[data-testid="notif-reset-filters"]').click()
    cy.wait('@getNotifications')
    cy.get('[data-testid="notif-view-cards"]').click()
    cy.get('[data-testid="notif-card-1"]').click({ force: true })
    cy.get('[data-testid="notif-detail-dialog"]').should('be.visible')
    cy.get('[data-testid="notif-copy-title"]').click()
    cy.get('@writeClipboardAfterReload').should('have.been.calledWith', '风控熔断')
    cy.get('[data-testid="notif-detail-next"]').click()
    cy.get('[data-testid="notif-detail-meta-extra"]').should('contain', '#2')
    cy.get('[data-testid="notif-detail-prev"]').click()
    cy.get('[data-testid="notif-detail-meta-extra"]').should('contain', '#1')
  })
})
