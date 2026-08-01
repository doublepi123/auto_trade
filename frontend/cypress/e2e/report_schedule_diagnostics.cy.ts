describe('Scheduled report diagnostics', () => {
  function visitStrategy() {
    cy.visitApp('/#/strategy')
    cy.contains('定时报告', { timeout: 10000 }).should('be.visible')
  }

  it('shows the passive schedule status with as-of semantics', () => {
    visitStrategy()
    cy.wait('@getReportScheduleStatus')

    cy.get('[data-testid="report-schedule-enabled-tag"]').should('contain', '已启用')
    cy.get('[data-testid="report-schedule-effective-symbol"]').should('contain', 'AAPL.US')
    cy.get('[data-testid="report-schedule-status"]').should('contain', '推送间隔 24 小时')
    cy.get('[data-testid="report-schedule-last-sent"]').should('contain', '2h前')
    cy.get('[data-testid="report-schedule-next-eligible"]').should('contain', '约 22 小时后')
    cy.get('[data-testid="report-schedule-asof"]').should('contain', '获取于')
    cy.get('[data-testid="report-schedule-asof"]').should('contain', '进程内')
  })

  it('shows disabled and never-run state truthfully', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/reports/schedule/status', {
      body: {
        enabled: false,
        configured_symbol: '',
        effective_symbol: 'AAPL.US',
        interval_hours: 24,
        has_process_send_history: false,
        last_sent_age_seconds: null,
        next_eligible_in_seconds: null,
        eligible_now: false,
        state_scope: 'process',
        resets_on_restart: true,
      },
    }).as('statusDisabled')

    cy.visit('/#/strategy')
    cy.wait('@statusDisabled')

    cy.get('[data-testid="report-schedule-enabled-tag"]').should('contain', '未启用')
    cy.get('[data-testid="report-schedule-last-sent"]').should('contain', '本次进程未发送')
    cy.get('[data-testid="report-schedule-next-eligible"]').should('contain', '未启用')
  })

  it('shows eligible-now state when the throttle window has passed', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/reports/schedule/status', {
      body: {
        enabled: true,
        configured_symbol: 'AAPL.US',
        effective_symbol: 'AAPL.US',
        interval_hours: 24,
        has_process_send_history: true,
        last_sent_age_seconds: 90000,
        next_eligible_in_seconds: 0,
        eligible_now: true,
        state_scope: 'process',
        resets_on_restart: true,
      },
    }).as('statusEligible')

    cy.visit('/#/strategy')
    cy.wait('@statusEligible')
    cy.get('[data-testid="report-schedule-next-eligible"]').should('contain', '现在可发送')
  })

  it('shows an error state and recovers via explicit refresh', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/reports/schedule/status', {
      statusCode: 500,
      body: { detail: 'status failed' },
    }).as('statusError')

    cy.visit('/#/strategy')
    cy.wait('@statusError')
    cy.get('[data-testid="report-schedule-status-error"]').should('be.visible')

    cy.intercept('GET', '/api/reports/schedule/status', {
      body: {
        enabled: true,
        configured_symbol: 'AAPL.US',
        effective_symbol: 'AAPL.US',
        interval_hours: 24,
        has_process_send_history: false,
        last_sent_age_seconds: null,
        next_eligible_in_seconds: null,
        eligible_now: true,
        state_scope: 'process',
        resets_on_restart: true,
      },
    }).as('statusRetry')
    cy.get('[data-testid="report-schedule-status-retry"]').click()
    cy.wait('@statusRetry')
    cy.get('[data-testid="report-schedule-enabled-tag"]').should('contain', '已启用')
  })

  it('fetches the preview only on explicit click and renders the content', () => {
    cy.stubApi()
    let previewCalls = 0
    cy.intercept('GET', '/api/reports/schedule/preview*', (req) => {
      previewCalls += 1
      req.reply({
        body: {
          symbol: 'AAPL.US',
          target_date: '2026-06-16',
          title: '交易日报 · AAPL.US · 2026-06-16',
          content: '成交 3 笔，实现盈亏 +250.00；胜率 66.7%。',
        },
      })
    }).as('preview')

    cy.visit('/#/strategy')
    cy.contains('定时报告', { timeout: 10000 }).should('be.visible')
    cy.wrap(null).then(() => {
      expect(previewCalls).to.equal(0)
    })

    cy.get('[data-testid="report-schedule-preview-open"]').click()
    cy.wait('@preview')
    cy.get('[data-testid="report-schedule-preview-dialog"]').should('be.visible')
    cy.get('[data-testid="report-schedule-preview-note"]').should('contain', '不发送通知')
    cy.get('[data-testid="report-schedule-preview-meta"]').should('contain', 'AAPL.US')
    cy.get('[data-testid="report-schedule-preview-meta"]').should('contain', '2026-06-16')
    cy.get('[data-testid="report-schedule-preview-title"]').should('contain', '交易日报 · AAPL.US · 2026-06-16')
    cy.get('[data-testid="report-schedule-preview-content"]').should('contain', '成交 3 笔')
    cy.wrap(null).then(() => {
      expect(previewCalls).to.equal(1)
    })
  })

  it('shows a preview error and recovers via explicit retry', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/reports/schedule/preview*', {
      statusCode: 400,
      body: { detail: 'invalid date' },
    }).as('previewError')

    cy.visit('/#/strategy')
    cy.get('[data-testid="report-schedule-preview-open"]').click()
    cy.wait('@previewError')
    cy.get('[data-testid="report-schedule-preview-error"]').should('be.visible')

    cy.intercept('GET', '/api/reports/schedule/preview*', {
      body: {
        symbol: 'AAPL.US',
        target_date: '2026-06-16',
        title: '交易日报 · AAPL.US · 2026-06-16',
        content: '成交 3 笔，实现盈亏 +250.00；胜率 66.7%。',
      },
    }).as('previewRetry')
    cy.get('[data-testid="report-schedule-preview-retry"]').click()
    cy.wait('@previewRetry')
    cy.get('[data-testid="report-schedule-preview-content"]').should('contain', '成交 3 笔')
  })

  it('preview and status refresh never call run-now or schedule mutations', () => {
    cy.stubApi()
    let runCalls = 0
    let putCalls = 0
    cy.intercept('POST', '/api/reports/schedule/run', (req) => {
      runCalls += 1
      req.reply({ body: { sent: true, symbol: 'AAPL.US', title: 't', error: null } })
    })
    cy.intercept('PUT', '/api/strategy', (req) => {
      putCalls += 1
      req.reply({ statusCode: 200, body: req.body })
    })

    cy.visit('/#/strategy')
    cy.contains('定时报告', { timeout: 10000 }).should('be.visible')
    cy.wait('@getReportScheduleStatus')

    cy.get('[data-testid="report-schedule-status-refresh"]').click()
    cy.wait('@getReportScheduleStatus')
    cy.get('[data-testid="report-schedule-preview-open"]').click()
    cy.wait('@getReportSchedulePreview')
    cy.get('[data-testid="report-schedule-preview-dialog"]').should('be.visible')
    cy.get('[data-testid="report-schedule-preview-dialog"]').contains('button', '关闭').click()

    cy.wrap(null).then(() => {
      expect(runCalls).to.equal(0)
      expect(putCalls).to.equal(0)
    })
  })

  it('keeps the run-now action working with its existing semantics', () => {
    visitStrategy()
    cy.get('[data-testid="report-schedule-test"]').click()
    cy.wait('@runScheduledReport')
    cy.contains('已发送').should('be.visible')
  })
})
