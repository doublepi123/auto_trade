describe('Reports Page', () => {
  function formatDate(date: Date): string {
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    return `${year}-${month}-${day}`
  }

  function daysAgo(days: number): string {
    const date = new Date()
    date.setDate(date.getDate() - days)
    return formatDate(date)
  }

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

  it('renders readonly report enhancements', () => {
    cy.intercept('GET', '/api/reports/range*', {
      statusCode: 200,
      body: makeReport({
        metrics: {
          total_pnl: 425,
          total_trades: 4,
          win_count: 3,
          loss_count: 1,
          win_rate: 0.75,
          profit_loss_ratio: 3.2,
          avg_pnl_per_trade: 106.25,
          max_profit: 500,
          max_loss: -120,
          max_drawdown: 120,
          llm_suggestions_count: 4,
          llm_applied_count: 2,
          llm_apply_rate: 0.5,
          llm_profitable_count: 1,
          llm_accuracy_rate: 0.5,
        },
        daily_points: [
          { date: '2024-01-15', pnl: 300, cumulative_pnl: 300, drawdown: 0, trade_count: 2, win_count: 2 },
          { date: '2024-01-16', pnl: -120, cumulative_pnl: 180, drawdown: 120, trade_count: 1, win_count: 0 },
          { date: '2024-01-17', pnl: 245, cumulative_pnl: 425, drawdown: 0, trade_count: 1, win_count: 1 },
        ],
        attribution: [
          { key: 'SELL', label: '平多', trade_count: 3, pnl: 545, win_rate: 0.6667, share: 0.75 },
          { key: 'BUY_TO_COVER', label: '平空', trade_count: 1, pnl: -120, win_rate: 0, share: 0.25 },
        ],
        details: [
          {
            date: '2024-01-15',
            orders: [
              { broker_order_id: 'ord-1', side: 'SELL', quantity: 10, executed_price: 130, status: 'FILLED', filled_at: '2024-01-15T15:00:00Z', pnl: 300 },
            ],
          },
        ],
      }),
    }).as('getEnhancedReport')

    cy.get('[data-testid="reports-preset-7d"]').click()
    cy.wait('@getEnhancedReport').then(({ request }) => {
      expect(request.query.from_date).to.eq(daysAgo(6))
      expect(request.query.to_date).to.eq(formatDate(new Date()))
    })
    cy.get('[data-testid="reports-preset-30d"]').click()
    cy.wait('@getEnhancedReport').then(({ request }) => {
      expect(request.query.from_date).to.eq(daysAgo(29))
    })
    cy.get('[data-testid="reports-preset-90d"]').click()
    cy.wait('@getEnhancedReport').then(({ request }) => {
      expect(request.query.from_date).to.eq(daysAgo(89))
    })

    cy.get('[data-testid="reports-query-summary"]').should('contain', 'AAPL.US')
    cy.get('[data-testid="reports-export-preview"]').should('contain', 'report_AAPL_US')
    cy.get('[data-testid="reports-insights"]').should('contain', '最佳日').and('contain', '2024-01-15').and('contain', '+$300.00')
    cy.get('[data-testid="reports-insights"]').should('contain', '最差日').and('contain', '2024-01-16').and('contain', '-$120.00')
    cy.get('[data-testid="reports-insights"]').should('contain', '盈利日/亏损日').and('contain', '2 / 1')
    cy.get('[data-testid="reports-attribution-table"]').should('contain', '平多').and('contain', '+$545.00').and('contain', '75.0%')
    cy.get('[data-testid="reports-daily-table"] .el-table__expand-icon').first().click()
    cy.get('[data-testid="reports-order-details"]').should('contain', 'ord-1').and('contain', 'SELL').and('contain', '+$300.00')
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
