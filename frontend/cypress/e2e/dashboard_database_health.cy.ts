describe('Dashboard database storage health', () => {
  function visitDashboard() {
    cy.visitApp('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
  }

  it('shows a file-backed healthy snapshot with usage proportion', () => {
    visitDashboard()
    cy.wait('@getDatabaseHealth')

    cy.get('[data-testid="dashboard-db-health"]').should('be.visible')
    cy.get('[data-testid="db-health-journal"]').should('contain', 'WAL')
    cy.get('[data-testid="db-health-size"]').should('contain', '4.7 MB')
    cy.get('[data-testid="dashboard-db-health"]').should('contain', '480 KB')
    cy.get('[data-testid="db-health-wal"]').should('contain', '200 KB')
    cy.get('[data-testid="db-health-usage"]').should('exist')
    cy.get('[data-testid="db-health-usage-label"]').should('contain', '1080 / 1200（90%）')
    cy.get('[data-testid="db-health-checked-at"]').should('contain', '检查于')
    cy.get('[data-testid="db-health-checked-at"]').should('contain', '刚刚')
  })

  it('handles in-memory / null WAL and missing metrics honestly', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/database-health', {
      body: {
        checked_at: new Date().toISOString(),
        dialect: 'sqlite',
        journal_mode: 'memory',
        page_size_bytes: 4096,
        page_count: 40,
        freelist_count: 0,
        used_page_count: 40,
        database_size_bytes: 163840,
        free_space_bytes: 0,
        wal_size_bytes: null,
      },
    }).as('dbHealthMemory')

    cy.visit('/')
    cy.wait('@dbHealthMemory')
    cy.get('[data-testid="db-health-journal"]').should('contain', 'MEMORY')
    cy.get('[data-testid="db-health-wal"]').should('contain', '不适用或未知')
    cy.get('[data-testid="db-health-usage-label"]').should('contain', '40 / 40（100%）')
  })

  it('omits the proportion bar when page counts are unavailable', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/database-health', {
      body: {
        checked_at: new Date().toISOString(),
        dialect: 'sqlite',
        journal_mode: null,
        page_size_bytes: null,
        page_count: null,
        freelist_count: null,
        used_page_count: null,
        database_size_bytes: null,
        free_space_bytes: null,
        wal_size_bytes: null,
      },
    }).as('dbHealthSparse')

    cy.visit('/')
    cy.wait('@dbHealthSparse')
    cy.get('[data-testid="dashboard-db-health"]').should('be.visible')
    cy.get('[data-testid="db-health-journal"]').should('contain', '—')
    cy.get('[data-testid="db-health-size"]').should('contain', '—')
    cy.get('[data-testid="db-health-usage"]').should('not.exist')
  })

  it('shows an error state and recovers via the diagnostics refresh', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/database-health', {
      statusCode: 500,
      body: { detail: 'health probe failed' },
    }).as('dbHealthError')

    cy.visit('/')
    cy.wait('@dbHealthError')
    cy.get('[data-testid="db-health-error"]').should('be.visible')

    cy.intercept('GET', '/api/database-health', (req) => {
      req.reply({
        body: {
          checked_at: new Date().toISOString(),
          dialect: 'sqlite',
          journal_mode: 'wal',
          page_size_bytes: 4096,
          page_count: 1200,
          freelist_count: 120,
          used_page_count: 1080,
          database_size_bytes: 4915200,
          free_space_bytes: 491520,
          wal_size_bytes: 204800,
        },
      })
    }).as('dbHealthRetry')
    cy.get('[data-testid="dash-diagnostics-refresh"]').click()
    cy.wait('@dbHealthRetry')
    cy.get('[data-testid="db-health-journal"]').should('contain', 'WAL')
  })

  it('ages the checked-at freshness label without a data refresh', () => {
    const now = new Date('2026-08-02T12:00:00Z').getTime()
    cy.clock(now, ['Date', 'setInterval', 'clearInterval'])
    cy.stubApi()
    cy.intercept('GET', '/api/database-health', {
      body: {
        checked_at: '2026-08-02T12:00:00Z',
        dialect: 'sqlite',
        journal_mode: 'wal',
        page_size_bytes: 4096,
        page_count: 1200,
        freelist_count: 120,
        used_page_count: 1080,
        database_size_bytes: 4915200,
        free_space_bytes: 491520,
        wal_size_bytes: 204800,
      },
    }).as('dbHealthClock')

    cy.visit('/')
    cy.wait('@dbHealthClock')
    cy.get('[data-testid="db-health-checked-at"]').should('contain', '刚刚')

    // Advance the clock: the label must age via the 1s tick alone (no refetch).
    cy.tick(5000)
    cy.get('[data-testid="db-health-checked-at"]').should('contain', '5s前')
    cy.get('@dbHealthClock.all').should('have.length', 1)
  })

  it('shows checked-at freshness relative to an older probe time', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/database-health', {
      body: {
        checked_at: new Date(Date.now() - 90 * 60 * 1000).toISOString(),
        dialect: 'sqlite',
        journal_mode: 'wal',
        page_size_bytes: 4096,
        page_count: 1200,
        freelist_count: 120,
        used_page_count: 1080,
        database_size_bytes: 4915200,
        free_space_bytes: 491520,
        wal_size_bytes: 0,
      },
    }).as('dbHealthOld')

    cy.visit('/')
    cy.wait('@dbHealthOld')
    cy.get('[data-testid="db-health-checked-at"]').should('contain', '检查于')
    cy.get('[data-testid="db-health-checked-at"]').should('contain', 'h前')
    cy.get('[data-testid="db-health-wal"]').should('contain', '0 B')
  })

  it('offers no maintenance actions and never calls mutating endpoints', () => {
    let mutatingCalls = 0
    cy.stubApi()
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
      cy.intercept(method, '/api/database-health*', (req) => {
        mutatingCalls += 1
        req.reply({ statusCode: 404, body: {} })
      })
    }

    cy.visit('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
    cy.wait('@getDatabaseHealth')

    cy.get('[data-testid="dashboard-db-health"]').within(() => {
      cy.get('button').should('not.exist')
      cy.contains('VACUUM').should('not.exist')
      cy.contains('清理').should('not.exist')
      cy.contains('修复').should('not.exist')
      cy.contains('检查点').should('not.exist')
    })

    cy.get('[data-testid="dash-diagnostics-refresh"]').click()
    cy.wrap(null).then(() => {
      expect(mutatingCalls).to.equal(0)
    })
  })

  it('keeps the card usable on a mobile viewport', () => {
    cy.viewport(390, 844)
    cy.stubApi()
    cy.visit('/')
    cy.wait('@getDatabaseHealth')
    cy.get('[data-testid="dashboard-db-health"]').should('be.visible')
    cy.get('[data-testid="db-health-journal"]').should('contain', 'WAL')
    cy.get('[data-testid="db-health-usage"]').should('exist')
    cy.get('body').then(($body) => {
      expect($body[0].scrollWidth).to.be.lte($body[0].clientWidth)
    })
  })
})
