describe('Alert firing history', () => {
  it('opens a rule and shows its firing timeline', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules', {
      body: {
        items: [
          {
            id: 7,
            name: 'AAPL 高点',
            symbol: 'AAPL.US',
            rule_type: 'price_above',
            threshold: 150,
            severity: 'WARNING',
            enabled: true,
            cooldown_seconds: 300,
            last_fired_at: '2026-06-16T03:00:00Z',
            created_at: '2026-06-15T00:00:00Z',
          },
        ],
        total: 1,
      },
    }).as('rules')
    cy.intercept('GET', '/api/alert-rules/7/history*', {
      body: {
        items: [
          {
            id: 2, rule_id: 7, symbol: 'AAPL.US', rule_type: 'price_above',
            threshold: 150, trigger_value: 160, severity: 'WARNING',
            message: 'AAPL.US 现价 160.00 ≥ 150.00', fired_at: '2026-06-16T03:00:00Z',
          },
          {
            id: 1, rule_id: 7, symbol: 'AAPL.US', rule_type: 'price_above',
            threshold: 150, trigger_value: 155, severity: 'WARNING',
            message: 'AAPL.US 现价 155.00 ≥ 150.00', fired_at: '2026-06-16T02:00:00Z',
          },
        ],
        total: 2,
      },
    }).as('history')

    cy.visit('/#/alerts')
    cy.wait('@rules')
    cy.get('[data-testid="alert-history"]').click()
    cy.wait('@history')

    cy.get('[data-testid="alert-history-dialog"]').should('be.visible')
    cy.contains('共 2 次触发').should('be.visible')
    cy.contains('160').should('be.visible')
    cy.contains('155').should('be.visible')
  })

  it('shows empty note when a rule has never fired', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules', {
      body: {
        items: [
          {
            id: 9, name: 'never', symbol: 'TSLA.US', rule_type: 'price_below',
            threshold: 200, severity: 'WARNING', enabled: true, cooldown_seconds: 300,
            last_fired_at: null, created_at: '2026-06-15T00:00:00Z',
          },
        ],
        total: 1,
      },
    }).as('rules')
    cy.intercept('GET', '/api/alert-rules/9/history*', { body: { items: [], total: 0 } }).as('history')

    cy.visit('/#/alerts')
    cy.wait('@rules')
    cy.get('[data-testid="alert-history"]').click()
    cy.wait('@history')
    cy.contains('该规则尚未触发过').should('be.visible')
  })

  it('shows alert firing observability summaries and filters', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules', {
      body: {
        items: [
          {
            id: 7,
            name: 'AAPL 高点',
            symbol: 'AAPL.US',
            rule_type: 'price_above',
            threshold: 150,
            severity: 'WARNING',
            enabled: true,
            cooldown_seconds: 300,
            last_fired_at: '2026-06-17T10:00:00Z',
            created_at: '2026-06-15T00:00:00Z',
          },
          {
            id: 8,
            name: 'TSLA 低点',
            symbol: 'TSLA.US',
            rule_type: 'price_below',
            threshold: 200,
            severity: 'CRITICAL',
            enabled: true,
            cooldown_seconds: 600,
            last_fired_at: '2026-06-16T09:00:00Z',
            created_at: '2026-06-15T00:00:00Z',
          },
          {
            id: 9,
            name: '日亏损',
            symbol: 'AAPL.US',
            rule_type: 'daily_loss',
            threshold: -500,
            severity: 'CRITICAL',
            enabled: false,
            cooldown_seconds: 900,
            last_fired_at: null,
            created_at: '2026-06-15T00:00:00Z',
          },
          {
            id: 10,
            name: '从未触发',
            symbol: 'MSFT.US',
            rule_type: 'price_above',
            threshold: 420,
            severity: 'INFO',
            enabled: true,
            cooldown_seconds: 300,
            last_fired_at: null,
            created_at: '2026-06-15T00:00:00Z',
          },
        ],
        total: 4,
      },
    }).as('rules')
    cy.intercept('GET', '/api/alert-rules/7/history*', {
      body: {
        items: [
          {
            id: 31,
            rule_id: 7,
            symbol: 'AAPL.US',
            rule_type: 'price_above',
            threshold: 150,
            trigger_value: 160,
            severity: 'WARNING',
            message: 'AAPL spike to 160.00',
            fired_at: '2026-06-17T10:00:00Z',
          },
          {
            id: 32,
            rule_id: 7,
            symbol: 'AAPL.US',
            rule_type: 'price_above',
            threshold: 150,
            trigger_value: 170,
            severity: 'WARNING',
            message: 'AAPL spike to 170.00',
            fired_at: '2026-06-17T09:00:00Z',
          },
          {
            id: 33,
            rule_id: 7,
            symbol: 'AAPL.US',
            rule_type: 'price_above',
            threshold: 150,
            trigger_value: 180,
            severity: 'CRITICAL',
            message: 'AAPL spike to 180.00',
            fired_at: '2026-06-17T08:00:00Z',
          },
        ],
        total: 3,
      },
    }).as('history')

    cy.visit('/#/alerts')
    cy.wait('@rules')

    cy.get('[data-testid="alert-rule-health"]').should('contain', '4')
    cy.get('[data-testid="alert-rule-health"]').should('contain', '3')
    cy.get('[data-testid="alert-rule-health"]').should('contain', '1')
    cy.get('[data-testid="alert-rule-health"]').should('contain', '2')
    cy.get('[data-testid="alert-rule-health"]').should('contain', '2')

    cy.get('[data-testid="alert-recent-firings"]').should('contain', 'AAPL 高点')
    cy.get('[data-testid="alert-recent-firings"]').should('contain', 'TSLA 低点')

    cy.get('[data-testid="alert-filter-never-fired"]').click()
    cy.get('.el-table__body').first().should('contain', '日亏损')
    cy.get('.el-table__body').first().should('contain', '从未触发')
    cy.get('.el-table__body').first().should('not.contain', 'AAPL 高点')

    cy.get('[data-testid="alert-filter-enabled"]').click()
    cy.get('.el-table__body').first().should('contain', '从未触发')
    cy.get('.el-table__body').first().should('not.contain', '日亏损')

    cy.contains('tr', 'AAPL 高点').within(() => {
      cy.get('[data-testid="alert-history"]').click()
    })
    cy.wait('@history')

    cy.get('[data-testid="alert-history-summary"]').should('contain', '3')
    cy.get('[data-testid="alert-history-summary"]').should('contain', '最近 100 条')
    cy.get('[data-testid="alert-history-summary"]').should('contain', '160')
    cy.get('[data-testid="alert-history-summary"]').should('contain', '170')
    cy.get('[data-testid="alert-history-summary"]').should('contain', '180')
    cy.get('[data-testid="alert-history-severity"]').should('contain', 'WARNING')
    cy.get('[data-testid="alert-history-severity"]').should('contain', '2')
    cy.get('[data-testid="alert-history-severity"]').should('contain', 'CRITICAL')
    cy.get('[data-testid="alert-history-severity"]').should('contain', '1')
    cy.get('[data-testid="alert-history-dialog"]').should('contain', 'AAPL spike')
  })
})
