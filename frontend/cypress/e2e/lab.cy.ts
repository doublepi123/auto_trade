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

  it('renders four tabs and version table', () => {
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

  it('renders LLM runtime observability', () => {
    cy.intercept('GET', '/api/strategy/llm-interval/status', {
      body: {
        enabled: true,
        interval_minutes: 2,
        last_analysis_at: '2026-06-17T10:00:00Z',
        next_analysis_at: '2026-06-17T10:02:00Z',
        current_suggestion: {
          buy_low: 180.5,
          sell_high: 188.8,
          confidence_score: 0.82,
          analysis: '震荡区间保持有效',
        },
        applied_values: { buy_low: 180.5, sell_high: 188.8 },
        reject_reason: null,
        budget: {
          max_symbols_per_cycle: 5,
          max_analyses_per_hour: 12,
          tracked_symbol_count: 3,
          effective_symbol_budget: 3,
          used_analyses_last_hour: 12,
          remaining_analyses_this_hour: 0,
        },
        symbol_statuses: [
          {
            symbol: 'AAPL.US', market: 'US', is_primary: true, has_pending_order: false,
            buy_cooldown_remaining_seconds: null, sell_cooldown_remaining_seconds: 30,
            last_analysis_at: '2026-06-17T10:00:00Z', next_analysis_at: '2026-06-17T10:02:00Z',
            last_status: 'SKIPPED', last_skip_reason: 'cooldown active',
          },
          {
            symbol: 'TSLA.US', market: 'US', is_primary: false, has_pending_order: true,
            buy_cooldown_remaining_seconds: null, sell_cooldown_remaining_seconds: null,
            last_analysis_at: null, next_analysis_at: null,
            last_status: null, last_skip_reason: null,
          },
        ],
      },
    }).as('runtimeStatus')
    cy.intercept('GET', '/api/strategy/llm-interval/interactions*', {
      body: [
        { id: 7, interaction_type: 'analyze', symbol: 'AAPL.US', market: 'US', success: true, error: '', order_action: 'BUY', order_status: 'SUBMITTED', order_id: 'ord-7', applied: true, created_at: '2026-06-17T10:00:00Z' },
        { id: 8, interaction_type: 'preview', symbol: 'TSLA.US', market: 'US', success: false, error: 'timeout', order_action: 'NONE', order_status: null, order_id: null, applied: false, created_at: '2026-06-17T09:55:00Z' },
      ],
    }).as('runtimeInteractions')

    cy.contains('.el-tabs__item', '运行状态').click()
    cy.wait(['@runtimeStatus', '@runtimeInteractions'])

    cy.get('[data-testid="llm-runtime-overview"]').should('contain', '已启用').and('contain', '180.50').and('contain', '188.80').and('contain', '震荡区间保持有效')
    cy.get('[data-testid="llm-runtime-budget"]').should('contain', '12').and('contain', '0')
    cy.get('[data-testid="llm-runtime-symbols"]').should('contain', 'AAPL.US').and('contain', 'cooldown active')
    cy.get('[data-testid="llm-runtime-interactions"]').should('contain', 'BUY').and('contain', 'timeout')
    cy.get('[data-testid="llm-runtime-health"]').should('contain', '预算已耗尽').and('contain', 'AAPL.US: cooldown active')
  })
})
