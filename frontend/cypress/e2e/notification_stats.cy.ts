describe('Notification delivery statistics', () => {
  const populatedStats = {
    from_date: null,
    to_date: null,
    total: 12,
    success: 9,
    failed: 3,
    success_rate: 75,
    by_severity: [
      { key: 'CRITICAL', total: 2, success: 1, failed: 1 },
      { key: 'INFO', total: 6, success: 6, failed: 0 },
      { key: 'WARNING', total: 4, success: 2, failed: 2 },
    ],
    failures_by_channel: [
      { key: 'serverchan', count: 1 },
      { key: 'webhook', count: 2 },
    ],
    daily: [
      { date: '2026-06-14', total: 4, success: 3, failed: 1 },
      { date: '2026-06-15', total: 5, success: 4, failed: 1 },
      { date: '2026-06-16', total: 3, success: 2, failed: 1 },
    ],
  }

  it('shows server-backed stats distinct from the paginated list', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/notifications/stats*', { body: populatedStats }).as('stats')

    cy.visit('/#/notifications')
    cy.wait('@stats')

    // Server population (12) is deliberately larger than the loaded page (3).
    cy.get('[data-testid="notif-stats-totals"]').should('contain', '发送 12')
    cy.get('[data-testid="notif-stats-totals"]').should('contain', '成功 9')
    cy.get('[data-testid="notif-stats-totals"]').should('contain', '失败 3')
    cy.get('[data-testid="notif-stats-totals"]').should('contain', '成功率 75%')
    cy.get('[data-testid="notif-summary"]').should('contain', '当前页 3/3')
    cy.get('[data-testid="notif-stats-scope"]').should('contain', '服务端全量日志（非当前页）')
    cy.get('[data-testid="notif-stats-scope"]').should('contain', '全部时间')

    cy.get('[data-testid="notif-stats-severity"]').should('contain', 'CRITICAL')
    cy.get('[data-testid="notif-stats-severity"]').should('contain', '2 条 · 成功 1 · 失败 1')
    cy.get('[data-testid="notif-stats-channels"]').should('contain', 'Server酱')
    cy.get('[data-testid="notif-stats-channels"]').should('contain', 'Webhook')
    cy.get('[data-testid="notif-stats-channels"]').should('contain', '仅统计失败记录')

    cy.get('[data-testid="notif-stats-trend-svg"]').should('exist')
    cy.get('[data-testid="notif-stats-trend-svg"] rect').should('have.length.at.least', 3)
    cy.get('[data-testid="notif-stats-trend"]').should('contain', '2026-06-14 ~ 2026-06-16')
  })

  it('shows an empty state when the window has no records', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/notifications/stats*', {
      body: {
        from_date: null,
        to_date: null,
        total: 0,
        success: 0,
        failed: 0,
        success_rate: 0,
        by_severity: [],
        failures_by_channel: [],
        daily: [],
      },
    }).as('statsEmpty')

    cy.visit('/#/notifications')
    cy.wait('@statsEmpty')
    cy.get('[data-testid="notif-stats-empty"]').should('be.visible')
    cy.get('[data-testid="notif-stats-trend"]').should('not.exist')
  })

  it('shows an error state and recovers via explicit retry of the stats query', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/notifications/stats*', {
      statusCode: 500,
      body: { detail: 'stats failed' },
    }).as('statsError')

    cy.visit('/#/notifications')
    cy.wait('@statsError')
    cy.get('[data-testid="notif-stats-error"]').should('be.visible')

    cy.intercept('GET', '/api/notifications/stats*', { body: populatedStats }).as('statsRetry')
    cy.get('[data-testid="notif-stats-retry"]').click()
    cy.wait('@statsRetry')
    cy.get('[data-testid="notif-stats-totals"]').should('contain', '发送 12')
  })

  it('refetches with the selected window and labels the echoed range', () => {
    cy.stubApi()
    const queries: Array<Record<string, string>> = []
    cy.intercept('GET', '/api/notifications/stats*', (req) => {
      queries.push({ ...req.query } as Record<string, string>)
      req.reply({
        body: {
          ...populatedStats,
          from_date: (req.query.from_date as string) ?? null,
          to_date: (req.query.to_date as string) ?? null,
        },
      })
    }).as('statsQuery')

    cy.visit('/#/notifications')
    cy.wait('@statsQuery')

    cy.get('[data-testid="notif-stats-range"] input[placeholder="统计开始"]').clear().type('2026-06-01')
    cy.get('[data-testid="notif-stats-range"] input[placeholder="统计结束"]').clear().type('2026-06-15')
    cy.get('h3').click()
    cy.get('[data-testid="notif-stats-reload"]').click()
    cy.wait('@statsQuery')

    cy.wrap(null).then(() => {
      expect(queries.length).to.be.at.least(2)
      const last = queries[queries.length - 1]
      expect(last.from_date).to.equal('2026-06-01')
      expect(last.to_date).to.equal('2026-06-15')
    })
    cy.get('[data-testid="notif-stats-scope"]').should('contain', '2026-06-01 至 2026-06-15')
  })

  it('never calls retry or test-send endpoints while viewing or filtering stats', () => {
    cy.stubApi()
    let retryCalls = 0
    let testSendCalls = 0
    cy.intercept('POST', '/api/notifications/*/retry', (req) => {
      retryCalls += 1
      req.reply({ statusCode: 500, body: {} })
    })
    cy.intercept('POST', '/api/credentials/notification-channels/test', (req) => {
      testSendCalls += 1
      req.reply({ statusCode: 500, body: {} })
    })
    cy.intercept('GET', '/api/notifications/stats*', { body: populatedStats }).as('stats')

    cy.visit('/#/notifications')
    cy.wait('@stats')

    cy.get('[data-testid="notif-stats-range"] input[placeholder="统计开始"]').clear().type('2026-06-10')
    cy.get('[data-testid="notif-stats-range"] input[placeholder="统计结束"]').clear().type('2026-06-16')
    cy.get('h3').click()
    cy.get('[data-testid="notif-stats-reload"]').click()
    cy.wait('@stats')
    cy.get('[data-testid="notif-stats-totals"]').should('contain', '发送 12')

    cy.wrap(null).then(() => {
      expect(retryCalls).to.equal(0)
      expect(testSendCalls).to.equal(0)
    })
  })

  it('keeps the stats section usable on a mobile viewport', () => {
    cy.viewport(390, 844)
    cy.stubApi()
    cy.intercept('GET', '/api/notifications/stats*', { body: populatedStats }).as('stats')

    cy.visit('/#/notifications')
    cy.wait('@stats')
    cy.get('[data-testid="notif-stats"]').should('be.visible')
    cy.get('[data-testid="notif-stats-totals"]').should('contain', '发送 12')
    cy.get('[data-testid="notif-stats-trend-svg"]').should('exist')
  })
})
