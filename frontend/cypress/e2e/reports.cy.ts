describe('Reports Page', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.visit('/#/reports')
  })

  const makeReport = (overrides: Partial<Record<string, unknown>> = {}) => ({
    period_type: 'range',
    symbol: 'AAPL.US',
    start_date: '2024-01-01',
    end_date: '2024-01-31',
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
    ...overrides,
  })

  it('should navigate to reports page', () => {
    cy.get('[data-testid="reports-view"]').should('be.visible')
    cy.contains('交易报告').should('be.visible')
  })

  it('should display empty state when no data', () => {
    cy.intercept('GET', '/api/reports/range*', {
      statusCode: 200,
      body: makeReport(),
    }).as('getReport')

    cy.get('[data-testid="reports-view"]').should('be.visible')
    cy.get('[data-testid="reports-symbol-input"] input').clear().type('AAPL.US')
    cy.get('[data-testid="reports-from-date"] input').clear().type('2024-01-01')
    cy.get('[data-testid="reports-to-date"] input').clear().type('2024-01-31')
    cy.get('[data-testid="reports-search"]').click()
    cy.wait('@getReport')
    cy.get('[data-testid="reports-view"]').contains('总盈亏').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('该报告区间没有日内交易记录').should('be.visible')
    cy.get('[data-testid="reports-export-json"]').should('not.be.disabled')
    cy.get('[data-testid="reports-export-csv"]').should('not.be.disabled')
  })

  it('should display report data with metrics', () => {
    cy.intercept('GET', '/api/reports/range*', {
      statusCode: 200,
      body: makeReport({
        metrics: {
          total_pnl: 1250.5,
          total_trades: 10,
          win_count: 7,
          loss_count: 3,
          win_rate: 0.7,
          profit_loss_ratio: 2.5,
          avg_pnl_per_trade: 125.05,
          max_profit: 500,
          max_loss: -200,
          max_drawdown: 150.25,
          llm_suggestions_count: 15,
          llm_applied_count: 12,
          llm_apply_rate: 0.8,
          llm_profitable_count: 9,
          llm_accuracy_rate: 0.75,
        },
        daily_points: [
          { date: '2024-01-15', pnl: 300, cumulative_pnl: 300, drawdown: 0, trade_count: 2, win_count: 2 },
          { date: '2024-01-16', pnl: -100, cumulative_pnl: 200, drawdown: 100, trade_count: 1, win_count: 0 },
          { date: '2024-01-17', pnl: 500, cumulative_pnl: 700, drawdown: 0, trade_count: 3, win_count: 3 },
        ],
      }),
    }).as('getReport')

    cy.get('[data-testid="reports-view"]').should('be.visible')
    cy.get('[data-testid="reports-symbol-input"] input').clear().type('AAPL.US')
    cy.get('[data-testid="reports-from-date"] input').clear().type('2024-01-01')
    cy.get('[data-testid="reports-to-date"] input').clear().type('2024-01-31')
    cy.get('[data-testid="reports-search"]').click()
    cy.wait('@getReport')
    cy.get('[data-testid="reports-view"]').contains('+$1250.50').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('10').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('70.0%').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('125.05').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('80.0%').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('75.0%').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('最大回撤').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('150.25').should('be.visible')
    cy.get('svg.pnl-chart polyline').should('exist')
    cy.get('[data-testid="reports-view"]').contains('累计盈亏').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('回撤').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('2024-01-15').should('be.visible')
    cy.get('[data-testid="reports-view"]').contains('+$300.00').should('be.visible')
  })

  it('should validate query conditions', () => {
    cy.get('[data-testid="reports-view"]').should('be.visible')
    cy.get('[data-testid="reports-symbol-input"] input').clear()
    cy.get('[data-testid="reports-search"]').click()
    cy.contains('.el-message', '请填写完整的查询条件').should('be.visible')

    cy.get('[data-testid="reports-symbol-input"] input').type('AAPL.US')
    cy.get('[data-testid="reports-from-date"] input').clear().type('2024-02-01')
    cy.get('[data-testid="reports-to-date"] input').clear().type('2024-01-31')
    cy.get('[data-testid="reports-search"]').click()
    cy.contains('.el-message', '开始日期不能晚于结束日期').should('be.visible')
  })

  it('should export report as json and csv', () => {
    cy.intercept('GET', '/api/reports/range*', {
      statusCode: 200,
      body: makeReport({
        metrics: {
          total_pnl: 1250.5,
          total_trades: 10,
          win_count: 7,
          loss_count: 3,
          win_rate: 0.7,
          profit_loss_ratio: 2.5,
          avg_pnl_per_trade: 125.05,
          max_profit: 500,
          max_loss: -200,
          max_drawdown: 150.25,
          llm_suggestions_count: 15,
          llm_applied_count: 12,
          llm_apply_rate: 0.8,
          llm_profitable_count: 9,
          llm_accuracy_rate: 0.75,
        },
        daily_points: [
          { date: '2024-01-15', pnl: 300, cumulative_pnl: 300, drawdown: 0, trade_count: 2, win_count: 2 },
        ],
      }),
    }).as('getReport')

    cy.get('[data-testid="reports-view"]').should('be.visible')
    cy.get('[data-testid="reports-symbol-input"] input').clear().type('AAPL.US')
    cy.get('[data-testid="reports-from-date"] input').clear().type('2024-01-01')
    cy.get('[data-testid="reports-to-date"] input').clear().type('2024-01-31')
    cy.get('[data-testid="reports-search"]').click()
    cy.wait('@getReport')

    cy.intercept('GET', '/api/reports/export*format=json*', { body: '{"ok":true}' }).as('exportJson')
    cy.intercept('GET', '/api/reports/export*format=csv*', { body: 'ok\n1' }).as('exportCsv')

    cy.get('[data-testid="reports-export-json"]').click()
    cy.wait('@exportJson')
    cy.get('[data-testid="reports-export-csv"]').click()
    cy.wait('@exportCsv')
  })
})
