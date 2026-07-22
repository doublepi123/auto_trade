describe('Statistics quality warnings', () => {
  const reportBody = (statisticsQuality?: Record<string, unknown>) => ({
    period_type: 'range',
    symbol: 'AAPL.US',
    start_date: '2026-07-01',
    end_date: '2026-07-17',
    metrics: {
      total_pnl: 0,
      total_trades: 0,
      win_count: 0,
      loss_count: 0,
      win_rate: 0,
      profit_loss_ratio: 0,
      avg_pnl_per_trade: 0,
      max_profit: 0,
      max_loss: 0,
      max_drawdown: 0,
      llm_suggestions_count: 0,
      llm_applied_count: 0,
      llm_apply_rate: 0,
      llm_profitable_count: 0,
      llm_accuracy_rate: 0,
    },
    daily_points: [],
    attribution: [],
    details: [],
    ...(statisticsQuality === undefined ? {} : { statistics_quality: statisticsQuality }),
  })

  const queryReport = () => {
    cy.visit('/#/reports')
    cy.get('[data-testid="reports-search"]').click()
    cy.wait('@qualityReport')
  }

  beforeEach(() => {
    cy.stubApi()
  })

  it('hides the alert when statistics are complete', () => {
    cy.intercept('GET', '/api/reports/range*', {
      body: reportBody({
        status: 'COMPLETE',
        known_exclusion_count: 0,
        unresolved_issue_count: 0,
        omitted_day_count: 0,
        items: [],
      }),
    }).as('qualityReport')

    queryReport()
    cy.get('[data-testid="statistics-quality-alert"]').should('not.exist')
  })

  it('warns when the service omits its quality contract', () => {
    cy.intercept('GET', '/api/reports/range*', {
      body: reportBody(),
    }).as('qualityReport')

    queryReport()
    cy.get('[data-testid="statistics-quality-alert"]')
      .should('have.attr', 'data-quality-status', 'UNKNOWN')
      .and('contain', '统计数据质量未知')
  })

  it('shows unresolved issue and omitted-day counts as an error', () => {
    cy.intercept('GET', '/api/reports/range*', {
      body: reportBody({
        status: 'UNRESOLVED',
        known_exclusion_count: 0,
        unresolved_issue_count: 2,
        omitted_day_count: 1,
        items: [],
      }),
    }).as('qualityReport')

    queryReport()
    cy.get('[data-testid="statistics-quality-alert"]')
      .should('have.attr', 'data-quality-status', 'UNRESOLVED')
      .and('contain', '已排除 1 个交易日')
      .and('contain', '发现 2 个待处理账本问题')
      .and('have.class', 'el-alert--error')
  })

  it('shows known historical exclusions as a warning', () => {
    cy.intercept('GET', '/api/reports/range*', {
      body: reportBody({
        status: 'KNOWN_EXCLUSIONS',
        known_exclusion_count: 3,
        unresolved_issue_count: 0,
        omitted_day_count: 0,
        items: [],
      }),
    }).as('qualityReport')

    queryReport()
    cy.get('[data-testid="statistics-quality-alert"]')
      .should('have.attr', 'data-quality-status', 'KNOWN_EXCLUSIONS')
      .and('contain', '统计已排除 3 笔已知历史数据')
      .and('have.class', 'el-alert--warning')
  })

  it('shows stale exclusions and omitted-day counts as an error', () => {
    cy.intercept('GET', '/api/reports/range*', {
      body: reportBody({
        status: 'STALE_EXCLUSION',
        known_exclusion_count: 1,
        unresolved_issue_count: 1,
        omitted_day_count: 2,
        items: [],
      }),
    }).as('qualityReport')

    queryReport()
    cy.get('[data-testid="statistics-quality-alert"]')
      .should('have.attr', 'data-quality-status', 'STALE_EXCLUSION')
      .and('contain', '已排除 2 个交易日')
      .and('contain', '发现 1 个待处理账本问题')
      .and('have.class', 'el-alert--error')
  })
})
