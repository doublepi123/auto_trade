interface EffectivenessItem {
  id: number
  name: string
  symbol: string
  rule_type: string
  threshold: number
  severity: string
  enabled: boolean
  cooldown_seconds: number
  created_at: string
  firing_count: number
  last_fired_at: string | null
  never_fired: boolean
}

function effectivenessItem(partial: Partial<EffectivenessItem> & { id: number; name: string }): EffectivenessItem {
  return {
    symbol: 'AAPL.US',
    rule_type: 'price_above',
    threshold: 150,
    severity: 'WARNING',
    enabled: true,
    cooldown_seconds: 300,
    created_at: '2026-06-15T00:00:00Z',
    firing_count: 0,
    last_fired_at: null,
    never_fired: true,
    ...partial,
  }
}

const populatedEffectiveness = {
  items: [
    effectivenessItem({
      id: 7, name: 'AAPL 高点', firing_count: 3,
      last_fired_at: '2026-06-17T10:00:00Z', never_fired: false,
    }),
    effectivenessItem({
      id: 9, name: '连续亏损告警', symbol: '', rule_type: 'consecutive_losses',
      threshold: 3, firing_count: 0, never_fired: true,
    }),
  ],
  total: 2,
}

describe('Alert rule effectiveness overview', () => {
  it('loads the server-backed effectiveness overview with window label on mount', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules/effectiveness*', {
      body: populatedEffectiveness,
    }).as('effectiveness')

    cy.visit('/#/alerts')
    cy.wait('@effectiveness')

    cy.get('[data-testid="alert-effectiveness-summary"]').should('contain', '规则总数 2')
    cy.get('[data-testid="alert-effectiveness-summary"]').should('contain', '窗口内触发 3 次')
    cy.get('[data-testid="alert-effectiveness-summary"]').should('contain', '窗口内有触发 1 条')
    cy.get('[data-testid="alert-effectiveness-summary"]').should('contain', '从未触发（全量） 1 条')
    cy.get('[data-testid="alert-effectiveness-window"]').should('contain', '统计窗口')

    cy.get('[data-testid="alert-effectiveness-table"]').should('contain', 'AAPL 高点')
    cy.get('[data-testid="alert-effectiveness-table"]').should('contain', '连续亏损告警')
    cy.get('[data-testid="alert-effectiveness-table"]').should('contain', '连续亏损 ≥')
    cy.get('[data-testid="alert-effectiveness-table"]').should('contain', '从未触发')
    cy.get('[data-testid="alert-effectiveness-table"]').should('contain', '有触发记录')
  })

  it('shows an empty state when no rules exist', () => {
    cy.stubApi()
    cy.visit('/#/alerts')
    cy.wait('@getAlertRuleEffectiveness')
    cy.get('[data-testid="alert-effectiveness-empty"]').should('be.visible')
  })

  it('shows an error state and recovers via explicit reload', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules/effectiveness*', {
      statusCode: 500,
      body: { detail: 'aggregate failed' },
    }).as('effectivenessError')

    cy.visit('/#/alerts')
    cy.wait('@effectivenessError')
    cy.get('[data-testid="alert-effectiveness-detail-error"]').should('be.visible')

    cy.intercept('GET', '/api/alert-rules/effectiveness*', {
      body: populatedEffectiveness,
    }).as('effectivenessRetry')
    cy.get('[data-testid="alert-effectiveness-retry"]').click()
    cy.wait('@effectivenessRetry')
    cy.get('[data-testid="alert-effectiveness-table"]').should('contain', 'AAPL 高点')
  })

  it('refetches with the selected window when the date range changes', () => {
    cy.stubApi()
    const queries: Array<Record<string, string>> = []
    cy.intercept('GET', '/api/alert-rules/effectiveness*', (req) => {
      queries.push({ ...req.query } as Record<string, string>)
      req.reply({ body: populatedEffectiveness })
    }).as('effectivenessQuery')

    cy.visit('/#/alerts')
    cy.wait('@effectivenessQuery')

    cy.get('[data-testid="alert-effectiveness-range"] input[placeholder="开始日期"]').clear().type('2026-06-01')
    cy.get('[data-testid="alert-effectiveness-range"] input[placeholder="结束日期"]').clear().type('2026-06-15')
    cy.get('h3').click()
    cy.get('[data-testid="alert-effectiveness-reload"]').click()
    cy.wait('@effectivenessQuery')

    cy.wrap(null).then(() => {
      expect(queries.length).to.be.at.least(2)
      const last = queries[queries.length - 1]
      expect(last.from_date).to.equal('2026-06-01')
      expect(last.to_date).to.equal('2026-06-15')
    })
  })

  it('keeps the effectiveness section usable on a mobile viewport', () => {
    cy.viewport(390, 844)
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules/effectiveness*', {
      body: populatedEffectiveness,
    }).as('effectiveness')

    cy.visit('/#/alerts')
    cy.wait('@effectiveness')
    cy.get('[data-testid="alert-effectiveness"]').should('be.visible')
    cy.get('[data-testid="alert-effectiveness-table"]').should('contain', 'AAPL 高点')
  })
})

describe('Alert rule account-wide rule types', () => {
  it('exposes consecutive_losses with account-wide symbol behavior and explicit save', () => {
    cy.stubApi()
    let postCount = 0
    let createdBody: Record<string, unknown> | null = null
    cy.intercept('POST', '/api/alert-rules', (req) => {
      postCount += 1
      createdBody = { ...req.body } as Record<string, unknown>
      req.reply({
        body: {
          id: 21,
          ...req.body,
          last_fired_at: null,
          created_at: '2026-06-16T12:00:00Z',
        },
      })
    }).as('createRule')

    cy.visit('/#/alerts')
    cy.get('[data-testid="alert-create"]').click()
    cy.get('[data-testid="alert-dialog"]').should('be.visible')

    cy.get('input[placeholder="规则名称"]').type('连续亏损告警')
    cy.get('[data-testid="alert-rule-type"]').click()
    cy.get('.el-select-dropdown:visible .el-select-dropdown__item')
      .contains('连续亏损')
      .click()

    // Account-wide: symbol input disabled with guidance; threshold integer >= 1.
    cy.get('[data-testid="alert-symbol"]').should('be.disabled')
    cy.get('[data-testid="alert-account-wide-hint"]').should('contain', '账户级')
    cy.get('[data-testid="alert-threshold"] input').should('have.value', '3')

    // Selecting a type must not persist anything by itself.
    cy.wrap(null).then(() => {
      expect(postCount).to.equal(0)
    })

    cy.get('[data-testid="alert-save"]').click()
    cy.wait('@createRule')
    cy.wrap(null).then(() => {
      expect(postCount).to.equal(1)
      expect(createdBody).to.not.equal(null)
      const body = createdBody as unknown as Record<string, unknown>
      expect(body.rule_type).to.equal('consecutive_losses')
      expect(body.symbol).to.equal('')
      expect(body.threshold).to.equal(3)
    })
  })

  it('exposes kill_switch_engaged with a fixed threshold of 1 and blank symbol', () => {
    cy.stubApi()
    let createdBody: Record<string, unknown> | null = null
    cy.intercept('POST', '/api/alert-rules', (req) => {
      createdBody = { ...req.body } as Record<string, unknown>
      req.reply({
        body: {
          id: 22,
          ...req.body,
          last_fired_at: null,
          created_at: '2026-06-16T12:00:00Z',
        },
      })
    }).as('createRule')

    cy.visit('/#/alerts')
    cy.get('[data-testid="alert-create"]').click()
    cy.get('input[placeholder="规则名称"]').type('熔断告警')
    cy.get('[data-testid="alert-rule-type"]').click()
    cy.get('.el-select-dropdown:visible .el-select-dropdown__item')
      .contains('熔断开关')
      .click()

    cy.get('[data-testid="alert-symbol"]').should('be.disabled')
    cy.get('[data-testid="alert-threshold-fixed"] input').should('have.value', '1')
    cy.get('[data-testid="alert-threshold-fixed"] input').should('be.disabled')

    cy.get('[data-testid="alert-save"]').click()
    cy.wait('@createRule')
    cy.wrap(null).then(() => {
      const body = createdBody as unknown as Record<string, unknown>
      expect(body.rule_type).to.equal('kill_switch_engaged')
      expect(body.symbol).to.equal('')
      expect(body.threshold).to.equal(1)
    })
  })

  it('blocks an invalid consecutive_losses threshold without posting', () => {
    cy.stubApi()
    let postCount = 0
    cy.intercept('POST', '/api/alert-rules', (req) => {
      postCount += 1
      req.reply({
        body: { id: 23, ...req.body, last_fired_at: null, created_at: '2026-06-16T12:00:00Z' },
      })
    }).as('createRule')

    cy.visit('/#/alerts')
    cy.get('[data-testid="alert-create"]').click()
    cy.get('input[placeholder="规则名称"]').type('连续亏损告警')
    cy.get('[data-testid="alert-rule-type"]').click()
    cy.get('.el-select-dropdown:visible .el-select-dropdown__item')
      .contains('连续亏损')
      .click()

    // Force an invalid value below the minimum, then attempt an explicit save.
    cy.get('[data-testid="alert-threshold"] input').clear().type('0')
    cy.get('[data-testid="alert-save"]').click()
    cy.contains('连续亏损阈值须为 ≥ 1 的整数').should('be.visible')
    cy.wrap(null).then(() => {
      expect(postCount).to.equal(0)
    })
  })

  it('edits an existing account-wide rule without touching the symbol', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/alert-rules', {
      body: {
        items: [
          {
            id: 31,
            name: '连续亏损告警',
            symbol: '',
            rule_type: 'consecutive_losses',
            threshold: 3,
            severity: 'CRITICAL',
            enabled: true,
            cooldown_seconds: 300,
            last_fired_at: null,
            created_at: '2026-06-15T00:00:00Z',
          },
        ],
        total: 1,
      },
    }).as('rules')
    let updatedBody: Record<string, unknown> | null = null
    cy.intercept('PUT', '/api/alert-rules/31', (req) => {
      updatedBody = { ...req.body } as Record<string, unknown>
      req.reply({ body: { id: 31, ...req.body, last_fired_at: null, created_at: '2026-06-15T00:00:00Z' } })
    }).as('updateRule')

    cy.visit('/#/alerts')
    cy.wait('@rules')
    cy.get('.el-table__body').first().should('contain', '账户级')
    cy.get('.el-table__body').first().should('contain', '连续亏损 ≥ 3')
    cy.contains('button', '编辑').click()

    cy.get('[data-testid="alert-dialog"]').should('be.visible')
    cy.get('[data-testid="alert-symbol"]').should('be.disabled')
    cy.get('[data-testid="alert-threshold"] input').clear().type('4')
    cy.get('[data-testid="alert-save"]').click()
    cy.wait('@updateRule')
    cy.wrap(null).then(() => {
      const body = updatedBody as unknown as Record<string, unknown>
      expect(body.rule_type).to.equal('consecutive_losses')
      expect(body.symbol).to.equal('')
      expect(body.threshold).to.equal(4)
    })
  })
})
