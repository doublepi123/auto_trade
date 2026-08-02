describe('Dashboard cron job health', () => {
  function visitDashboard() {
    cy.visitApp('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
  }

  it('shows enabled, disabled and pending jobs with distinct statuses', () => {
    visitDashboard()
    cy.wait('@getCronHealth')

    cy.get('[data-testid="dashboard-cron-health"]').should('be.visible')
    cy.get('[data-testid="cron-health-checked-at"]').should('contain', '快照')
    cy.get('[data-testid="cron-health-summary"]').should('contain', '共 3 个任务')
    cy.get('[data-testid="cron-health-summary"]').should('contain', '健康 1')
    cy.get('[data-testid="cron-health-summary"]').should('contain', '已禁用 1')
    cy.get('[data-testid="cron-health-summary"]').should('contain', '等待中 1')

    cy.get('[data-testid="cron-health-job-notification_retry"]').within(() => {
      cy.get('[data-testid="cron-job-status"]').should('contain', '健康')
      cy.get('[data-testid="cron-job-enabled"]').should('contain', '已启用')
      cy.get('[data-testid="cron-job-interval"]').should('contain', '1 分钟')
      cy.get('[data-testid="cron-job-outcome"]').should('contain', '成功')
      cy.get('[data-testid="cron-job-ticks"]').should('contain', '240 / 0')
      cy.get('[data-testid="cron-job-last-success"]').should('not.contain', '—')
      cy.get('[data-testid="cron-job-last-failure"]').should('contain', '—')
      cy.get('[data-testid="cron-job-failure-code"]').should('not.exist')
    })

    cy.get('[data-testid="cron-health-job-daily_report"]').within(() => {
      cy.get('[data-testid="cron-job-status"]').should('contain', '已禁用')
      cy.get('[data-testid="cron-job-enabled"]').should('contain', '已禁用')
      cy.get('[data-testid="cron-job-outcome"]').should('contain', '暂无')
      cy.get('[data-testid="cron-job-ticks"]').should('contain', '0 / 0')
    })

    cy.get('[data-testid="cron-health-job-universe_rotation"]').within(() => {
      cy.get('[data-testid="cron-job-status"]').should('contain', '等待中')
      // enabled=null must surface as unknown, never guessed enabled/disabled.
      cy.get('[data-testid="cron-job-enabled"]').should('contain', '未知')
      cy.get('[data-testid="cron-job-interval"]').should('contain', '5 分钟')
    })
  })

  it('shows a failing stale job with failure code, counts and both badges', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/cron-health', {
      body: {
        as_of: new Date().toISOString(),
        jobs: [
          {
            name: 'order_reconcile',
            enabled: true,
            expected_interval_seconds: 30,
            last_success_at: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
            last_failure_at: new Date(Date.now() - 60 * 1000).toISOString(),
            last_failure_code: 'TimeoutError',
            tick_count: 88,
            failure_count: 3,
            last_outcome: 'failure',
            stale: true,
            status: 'failing',
          },
          {
            name: 'stale_loop',
            enabled: true,
            expected_interval_seconds: 120,
            last_success_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
            last_failure_at: null,
            last_failure_code: null,
            tick_count: 12,
            failure_count: 0,
            last_outcome: 'success',
            stale: true,
            status: 'stale',
          },
        ],
      },
    }).as('cronHealthFailing')

    cy.visit('/')
    cy.wait('@cronHealthFailing')

    cy.get('[data-testid="cron-health-summary"]').should('contain', '失败 1')
    cy.get('[data-testid="cron-health-summary"]').should('contain', '过期 1')
    // Stale is its own dimension: both stale=true jobs count, independent of verdict.
    cy.get('[data-testid="cron-health-stale-count"]').should('contain', '心跳过期 2')

    cy.get('[data-testid="cron-health-job-order_reconcile"]').within(() => {
      cy.get('[data-testid="cron-job-status"]').should('contain', '失败')
      // Staleness is a separate dimension: failing + stale shows both badges.
      cy.get('[data-testid="cron-job-stale"]').should('contain', '心跳过期')
      cy.get('[data-testid="cron-job-outcome"]').should('contain', '失败')
      cy.get('[data-testid="cron-job-ticks"]').should('contain', '88 / 3')
      cy.get('[data-testid="cron-job-failure-code"]').should('contain', 'TimeoutError')
      cy.get('[data-testid="cron-job-last-failure"]').should('not.contain', '—')
      cy.get('[data-testid="cron-job-last-success"]').should('not.contain', '—')
    })

    cy.get('[data-testid="cron-health-job-stale_loop"]').within(() => {
      cy.get('[data-testid="cron-job-status"]').should('contain', '过期')
      // The stale badge is not duplicated when the verdict itself is stale.
      cy.get('[data-testid="cron-job-stale"]').should('not.exist')
      cy.get('[data-testid="cron-job-failure-code"]').should('not.exist')
    })
  })

  it('never reports a disabled job as stale', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/cron-health', {
      body: {
        as_of: new Date().toISOString(),
        jobs: [
          {
            name: 'disabled_loop',
            enabled: false,
            expected_interval_seconds: 60,
            last_success_at: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
            last_failure_at: null,
            last_failure_code: null,
            tick_count: 5,
            failure_count: 0,
            last_outcome: 'success',
            stale: false,
            status: 'disabled',
          },
        ],
      },
    }).as('cronHealthDisabled')

    cy.visit('/')
    cy.wait('@cronHealthDisabled')
    cy.get('[data-testid="cron-health-job-disabled_loop"]').within(() => {
      cy.get('[data-testid="cron-job-status"]').should('contain', '已禁用')
      cy.get('[data-testid="cron-job-stale"]').should('not.exist')
    })
    cy.get('[data-testid="dashboard-cron-health"]').should('not.contain', '过期')
  })

  it('shows an empty state when no jobs are registered', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/cron-health', {
      body: { as_of: new Date().toISOString(), jobs: [] },
    }).as('cronHealthEmpty')

    cy.visit('/')
    cy.wait('@cronHealthEmpty')
    cy.get('[data-testid="cron-health-empty"]').should('contain', '暂无已注册的定时任务')
    cy.get('[data-testid="cron-health-summary"]').should('not.exist')
  })

  it('shows an error state and recovers via the diagnostics refresh', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/cron-health', {
      statusCode: 500,
      body: { detail: 'cron health probe failed' },
    }).as('cronHealthError')

    cy.visit('/')
    cy.wait('@cronHealthError')
    cy.get('[data-testid="cron-health-error"]').should('contain', 'cron health probe failed')

    cy.intercept('GET', '/api/cron-health', (req) => {
      const now = new Date().toISOString()
      req.reply({
        body: {
          as_of: now,
          jobs: [
            {
              name: 'notification_retry',
              enabled: true,
              expected_interval_seconds: 60,
              last_success_at: now,
              last_failure_at: null,
              last_failure_code: null,
              tick_count: 241,
              failure_count: 0,
              last_outcome: 'success',
              stale: false,
              status: 'healthy',
            },
          ],
        },
      })
    }).as('cronHealthRetry')
    cy.get('[data-testid="dash-diagnostics-refresh"]').click()
    cy.wait('@cronHealthRetry')
    cy.get('[data-testid="cron-health-job-notification_retry"]').should('contain', '健康')
    cy.get('[data-testid="cron-health-error"]').should('not.exist')
  })

  it('renders a true unknown-status job distinctly from pending and failing', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/cron-health', {
      body: {
        as_of: new Date().toISOString(),
        jobs: [
          {
            name: 'adhoc_loop',
            enabled: true,
            expected_interval_seconds: null,
            last_success_at: null,
            last_failure_at: null,
            last_failure_code: null,
            tick_count: 0,
            failure_count: 0,
            last_outcome: '',
            stale: false,
            status: 'unknown',
          },
        ],
      },
    }).as('cronHealthUnknown')

    cy.visit('/')
    cy.wait('@cronHealthUnknown')
    cy.get('[data-testid="cron-health-summary"]').should('contain', '未知 1')
    cy.get('[data-testid="cron-health-job-adhoc_loop"]').within(() => {
      cy.get('[data-testid="cron-job-status"]').should('contain', '未知')
      cy.get('[data-testid="cron-job-interval"]').should('contain', '未知')
      cy.get('[data-testid="cron-job-outcome"]').should('contain', '暂无')
      cy.get('[data-testid="cron-job-stale"]').should('not.exist')
    })
    cy.get('[data-testid="cron-health-stale-count"]').should('not.exist')
  })

  it('offers no job controls and never calls mutating endpoints', () => {
    let mutatingCalls = 0
    cy.stubApi()
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
      cy.intercept(method, '/api/**', (req) => {
        mutatingCalls += 1
        req.reply({ statusCode: 404, body: {} })
      })
    }

    cy.visit('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
    cy.wait('@getCronHealth')

    cy.get('[data-testid="dashboard-cron-health"]').within(() => {
      cy.get('button').should('not.exist')
      cy.contains('运行').should('not.exist')
      cy.contains('重启').should('not.exist')
      cy.contains('注册').should('not.exist')
    })

    cy.get('[data-testid="dash-diagnostics-refresh"]').click()
    cy.wait('@getCronHealth')
    cy.wrap(null).then(() => {
      expect(mutatingCalls).to.equal(0)
    })
  })

  it('keeps the panel usable on a mobile viewport', () => {
    cy.viewport(390, 844)
    cy.stubApi()
    cy.visit('/')
    cy.wait('@getCronHealth')
    cy.get('[data-testid="dashboard-cron-health"]').should('be.visible')
    cy.get('[data-testid="cron-health-job-notification_retry"]').should('be.visible')
    cy.get('body').then(($body) => {
      expect($body[0].scrollWidth).to.be.lte($body[0].clientWidth)
    })
  })
})
