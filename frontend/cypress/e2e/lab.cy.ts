describe('LLM Lab workbench', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.intercept('GET', '/api/experiments/versions', {
      body: [
        {
          id: 1,
          name: 'baseline',
          version: 'v1',
          description: 'd',
          template: 'TPL',
          is_active: true,
          created_at: '2026-05-29T00:00:00Z',
        },
      ],
    }).as('versions')
    cy.intercept('GET', '/api/experiments', { body: ['exp1'] }).as('expNames')
    cy.intercept('GET', '/api/experiments/exp1/summary', { body: [] }).as('summary')
    cy.intercept('GET', '/api/performance/stats*', {
      body: { total_trades: 3, win_rate: 0.66, total_pnl: 12, avg_pnl: 4 },
    }).as('stats')
    cy.intercept('GET', '/api/performance/compare*', {
      body: [{ variant: 'A', total_trades: 3, win_rate: 0.66, total_pnl: 12, avg_pnl: 4 }],
    }).as('compare')
    cy.intercept('GET', '/api/performance/recommendations*', {
      body: ['变体 A 表现优秀'],
    }).as('recs')
    cy.visit('/#/lab')
  })

  it('renders three tabs and version table', () => {
    cy.get('[data-testid="lab-tabs"]').should('exist')
    cy.wait('@versions')
    cy.get('[data-testid="versions-table"]').should('contain', 'baseline')
  })

  it('loads performance when experiment selected', () => {
    cy.contains('.el-tabs__item', '性能看板').click()
    cy.get('[data-testid="perf-exp-select"]').click()
    cy.get('.el-select-dropdown__item:visible').contains('exp1').click()
    cy.wait(['@stats', '@compare', '@recs'])
    cy.get('[data-testid="perf-variants"]').should('contain', 'A')
    cy.get('[data-testid="perf-recommendations"]').should('contain', '表现优秀')
  })

  it('shows watermark when indicators unavailable', () => {
    cy.intercept('GET', '/api/indicators*', {
      body: {
        available: false,
        symbol: 'AAPL.US',
        market: 'US',
        atr: null,
        rsi: null,
        macd: null,
        volume_analysis: null,
        sentiment: null,
        multi_timeframe: null,
        bb_upper: null,
        bb_middle: null,
        bb_lower: null,
      },
    }).as('ind')
    cy.contains('.el-tabs__item', '指标面板').click()
    cy.get('[data-testid="load-indicators-btn"]').click()
    cy.wait('@ind')
    cy.get('[data-testid="indicators-unavailable"]').should('exist')
  })

  it('renders indicator cards when available', () => {
    cy.intercept('GET', '/api/indicators*', {
      body: {
        available: true,
        symbol: 'AAPL.US',
        market: 'US',
        atr: 1.2,
        rsi: 55.3,
        macd: { macd: 0.5, signal: 0.3, histogram: 0.2 },
        volume_analysis: { avg_volume: 1000, volume_ratio: 1.1, trend: 'normal' },
        sentiment: { sentiment: 'bullish', score: 0.6, description: '偏多' },
        multi_timeframe: {
          daily_trend: 'up',
          minute_trend: 'up',
          aligned: true,
          description: '日线趋势: up, 分钟趋势: up, 趋势一致',
        },
        bb_upper: 110,
        bb_middle: 100,
        bb_lower: 90,
      },
    }).as('ind')
    cy.contains('.el-tabs__item', '指标面板').click()
    cy.get('[data-testid="load-indicators-btn"]').click()
    cy.wait('@ind')
    cy.get('[data-testid="indicators-grid"]').should('contain', '55.30')
  })
})
