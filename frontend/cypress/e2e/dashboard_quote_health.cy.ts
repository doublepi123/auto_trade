describe('Dashboard quote stream health', () => {
  function visitDashboard() {
    cy.visitApp('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
  }

  it('shows a healthy stream with symbol, window metrics and lifetime counters', () => {
    // Freeze the clock: the quote age advances with the shared 1s ticker, so
    // exact age assertions need a fixed "now".
    cy.clock(new Date('2026-08-02T12:00:00Z').getTime(), ['Date', 'setInterval', 'clearInterval'])
    visitDashboard()
    cy.wait('@getQuoteStreamHealth')

    cy.get('[data-testid="dashboard-quote-health"]').should('be.visible')
    cy.get('[data-testid="quote-health-status"]').should('contain', '健康')
    cy.get('[data-testid="quote-health-checked-at"]').should('contain', '快照')
    cy.get('[data-testid="quote-health-symbol"]').should('contain', 'NVDA.US')
    cy.get('[data-testid="quote-health-subscription"]').should('contain', '已订阅')
    cy.get('[data-testid="quote-health-age"]').should('contain', '0.8s')
    cy.get('[data-testid="quote-health-last-timestamp"]').should('contain', '时间')
    cy.get('[data-testid="quote-health-received"]').should('contain', '1523')
    cy.get('[data-testid="quote-health-max-gap"]').should('contain', '4.2s')
    cy.get('[data-testid="quote-health-disconnects"]').should('contain', '2')
    cy.get('[data-testid="quote-health-resubscribes"]').should('contain', '3')
    cy.get('[data-testid="quote-health-retries"]').should('contain', '1')
    // The simple diagnostics push-age blocks stay untouched (augmented, not duplicated).
    cy.get('[data-testid="dashboard-diagnostics"]').should('contain', '最近推送')
  })

  it('shows a stale stream with the server verdict and an age past the threshold', () => {
    cy.clock(new Date('2026-08-02T12:00:00Z').getTime(), ['Date', 'setInterval', 'clearInterval'])
    cy.stubApi()
    cy.intercept('GET', '/api/quote-health', {
      body: {
        symbol: 'NVDA.US',
        quotes_received: 640,
        last_quote_timestamp: '2026-08-02T11:57:55Z',
        last_quote_age_seconds: 125.4,
        max_gap_seconds: 96.5,
        disconnect_count: 1,
        resubscribe_count: 2,
        disconnect_retry_count: 0,
        quotes_subscribed: true,
        status: 'stale',
        as_of: '2026-08-02T12:00:00Z',
      },
    }).as('quoteHealthStale')

    cy.visit('/')
    cy.wait('@quoteHealthStale')
    cy.get('[data-testid="quote-health-status"]').should('contain', '过期')
    cy.get('[data-testid="quote-health-age"]').should('contain', '2m 5s')
    cy.get('[data-testid="quote-health-max-gap"]').should('contain', '96.5s')
    cy.get('[data-testid="quote-health-subscription"]').should('contain', '已订阅')
  })

  it('shows the waiting state when subscribed but no push quote has arrived', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/quote-health', {
      body: {
        symbol: 'NVDA.US',
        quotes_received: 0,
        last_quote_timestamp: null,
        last_quote_age_seconds: null,
        max_gap_seconds: 0,
        disconnect_count: 0,
        resubscribe_count: 1,
        disconnect_retry_count: 0,
        quotes_subscribed: true,
        status: 'waiting',
        as_of: new Date().toISOString(),
      },
    }).as('quoteHealthWaiting')

    cy.visit('/')
    cy.wait('@quoteHealthWaiting')
    cy.get('[data-testid="quote-health-status"]').should('contain', '等待首条报价')
    cy.get('[data-testid="quote-health-age"]').should('contain', '—')
    cy.get('[data-testid="quote-health-waiting-note"]').should('contain', '等待当前窗口首条推送报价')
    cy.get('[data-testid="quote-health-received"]').should('contain', '0')
  })

  it('shows a known unsubscribed stream (HTTP 200) distinct from no-runtime', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/quote-health', {
      body: {
        symbol: 'NVDA.US',
        quotes_received: 980,
        last_quote_timestamp: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
        last_quote_age_seconds: 300.2,
        max_gap_seconds: 12.1,
        disconnect_count: 4,
        resubscribe_count: 5,
        disconnect_retry_count: 3,
        quotes_subscribed: false,
        status: 'unavailable',
        as_of: new Date().toISOString(),
      },
    }).as('quoteHealthUnsubscribed')

    cy.visit('/')
    cy.wait('@quoteHealthUnsubscribed')
    cy.get('[data-testid="quote-health-status"]').should('contain', '未订阅')
    cy.get('[data-testid="quote-health-subscription"]').should('contain', '未订阅')
    // A known stream keeps its observed counters — not an empty 503 shell.
    cy.get('[data-testid="quote-health-disconnects"]').should('contain', '4')
    cy.get('[data-testid="quote-health-resubscribes"]').should('contain', '5')
    cy.get('[data-testid="quote-health-retries"]').should('contain', '3')
    cy.get('[data-testid="quote-health-unavailable"]').should('not.exist')
  })

  it('renders the typed HTTP 503 unavailable body truthfully, not as an error', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/quote-health', {
      statusCode: 503,
      body: {
        symbol: '',
        quotes_received: 0,
        last_quote_timestamp: null,
        last_quote_age_seconds: null,
        max_gap_seconds: 0,
        disconnect_count: 0,
        resubscribe_count: 0,
        disconnect_retry_count: 0,
        quotes_subscribed: false,
        status: 'unavailable',
        as_of: new Date().toISOString(),
      },
    }).as('quoteHealth503')

    cy.visit('/')
    cy.wait('@quoteHealth503')
    cy.get('[data-testid="quote-health-status"]').should('contain', '不可用')
    cy.get('[data-testid="quote-health-unavailable"]').should('contain', '运行器尚未就绪')
    cy.get('[data-testid="quote-health-unavailable"]').should('contain', 'HTTP 503')
    cy.get('[data-testid="quote-health-unavailable"]').should('contain', '不是断线证据')
    cy.get('[data-testid="quote-health-checked-at"]').should('contain', '快照')
    // The zeroed placeholder counters of the 503 body must not be presented.
    cy.get('[data-testid="quote-health-received"]').should('not.exist')
    cy.get('[data-testid="quote-health-error"]').should('not.exist')
  })

  it('shows an error state and recovers via the diagnostics refresh', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/quote-health', {
      statusCode: 500,
      body: { detail: 'quote health probe failed' },
    }).as('quoteHealthError')

    cy.visit('/')
    cy.wait('@quoteHealthError')
    cy.get('[data-testid="quote-health-error"]').should('contain', 'quote health probe failed')

    cy.intercept('GET', '/api/quote-health', (req) => {
      req.reply({
        body: {
          symbol: 'NVDA.US',
          quotes_received: 12,
          last_quote_timestamp: new Date().toISOString(),
          last_quote_age_seconds: 1.5,
          max_gap_seconds: 3.3,
          disconnect_count: 0,
          resubscribe_count: 1,
          disconnect_retry_count: 0,
          quotes_subscribed: true,
          status: 'healthy',
          as_of: new Date().toISOString(),
        },
      })
    }).as('quoteHealthRetry')
    cy.get('[data-testid="dash-diagnostics-refresh"]').click()
    cy.wait('@quoteHealthRetry')
    cy.get('[data-testid="quote-health-status"]').should('contain', '健康')
    cy.get('[data-testid="quote-health-error"]').should('not.exist')
  })

  it('advances the quote age with the shared ticker without a refetch', () => {
    const now = new Date('2026-08-02T12:00:00Z').getTime()
    cy.clock(now, ['Date', 'setInterval', 'clearInterval'])
    cy.stubApi()
    cy.intercept('GET', '/api/quote-health', {
      body: {
        symbol: 'NVDA.US',
        quotes_received: 100,
        last_quote_timestamp: '2026-08-02T11:59:55Z',
        last_quote_age_seconds: 5,
        max_gap_seconds: 2,
        disconnect_count: 0,
        resubscribe_count: 1,
        disconnect_retry_count: 0,
        quotes_subscribed: true,
        status: 'healthy',
        as_of: '2026-08-02T12:00:00Z',
      },
    }).as('quoteHealthClock')

    cy.visit('/')
    cy.wait('@quoteHealthClock')
    cy.get('[data-testid="quote-health-age"]').should('contain', '5.0s')

    cy.tick(4000)
    cy.get('[data-testid="quote-health-age"]').should('contain', '9.0s')
    cy.get('@quoteHealthClock.all').should('have.length', 1)
  })

  it('offers no stream controls and only ever issues GET requests', () => {
    let mutatingCalls = 0
    cy.stubApi()
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
      cy.intercept(method, '/api/quote-health*', (req) => {
        mutatingCalls += 1
        req.reply({ statusCode: 404, body: {} })
      })
    }

    cy.visit('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
    cy.wait('@getQuoteStreamHealth')

    cy.get('[data-testid="dashboard-quote-health"]').within(() => {
      cy.get('button').should('not.exist')
      cy.contains('重连').should('not.exist')
      cy.contains('重置').should('not.exist')
    })

    cy.get('[data-testid="dash-diagnostics-refresh"]').click()
    cy.wait('@getQuoteStreamHealth')
    cy.wrap(null).then(() => {
      expect(mutatingCalls).to.equal(0)
    })
  })

  it('keeps the panel usable on a mobile viewport', () => {
    cy.viewport(390, 844)
    cy.stubApi()
    cy.visit('/')
    cy.wait('@getQuoteStreamHealth')
    cy.get('[data-testid="dashboard-quote-health"]').should('be.visible')
    cy.get('[data-testid="quote-health-symbol"]').should('contain', 'NVDA.US')
    cy.get('body').then(($body) => {
      expect($body[0].scrollWidth).to.be.lte($body[0].clientWidth)
    })
  })
})
