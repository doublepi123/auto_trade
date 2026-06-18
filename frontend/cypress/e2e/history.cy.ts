describe('History', () => {
  function visitHistory() {
    cy.visitApp('/#/history')
    cy.get('h3', { timeout: 10000 }).should('contain', '今日订单')
  }

  it('displays trade history page', () => {
    visitHistory()
    cy.get('h3').should('contain', '今日订单')
  })

  it('has refresh button', () => {
    visitHistory()
    cy.contains('button', '刷新').should('be.visible')
  })

  it('refreshes today orders from the broker when requested by the user', () => {
    cy.stubApi()
    cy.intercept('GET', /\/api\/orders\?.*refresh=true/, {
      body: {
        items: [],
        total: 0,
        page: 1,
        page_size: 10,
        scope: 'today',
      },
    }).as('refreshTodayOrders')

    cy.visit('/#/history')
    cy.contains('button', '刷新').click()
    cy.wait('@refreshTodayOrders')
  })

  it('loads paginated today orders by default and can cancel any live order', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/orders*', {
      body: {
        items: [
          {
            id: 0,
            broker_order_id: 'manual-1',
            symbol: 'NVDA.US',
            side: 'SELL',
            quantity: 3,
            price: 220.5,
            executed_quantity: 0,
            executed_price: 0,
            status: 'SUBMITTED',
            created_at: '2026-05-22T13:00:00Z',
            filled_at: null,
            source: 'broker',
            cancellable: true,
          },
        ],
        total: 1,
        page: 1,
        page_size: 10,
        scope: 'today',
      },
    }).as('todayOrders')
    cy.intercept('POST', '/api/orders/manual-1/cancel', {
      body: {
        broker_order_id: 'manual-1',
        status: 'CANCELLED',
        message: 'order cancelled',
      },
    }).as('cancelOrder')

    cy.visit('/#/history')
    cy.wait('@todayOrders')

    cy.contains('今日订单').should('be.visible')
    cy.contains('manual-1').should('be.visible')
    cy.contains('broker').should('be.visible')
    cy.contains('button', '撤单').click()
    cy.wait('@cancelOrder')
    cy.contains('撤单成功').should('be.visible')
  })

  it('renders trade-note analytics when notes exist', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/trade-notes', {
      body: { items: [], total: 0, page: 1, page_size: 50 },
    }).as('tn')
    cy.intercept('GET', '/api/trade-notes/analytics', {
      body: { total: 2, rated_count: 2, avg_rating: 4, rating_distribution: { 1: 0, 2: 0, 3: 1, 4: 0, 5: 1 }, top_tags: [{ tag: 'good', count: 2 }], distinct_symbols: 1 },
    }).as('tna')
    cy.visit('/#/history')
    cy.wait('@tna')
    cy.get('[data-testid="note-analytics"]').should('be.visible')
    cy.contains('热门标签').should('be.visible')
  })

  it('attaches a journal note to a persisted order', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/orders*', {
      body: {
        items: [
          {
            id: 42,
            broker_order_id: 'ord-42',
            symbol: 'AAPL.US',
            side: 'BUY',
            quantity: 10,
            price: 100,
            executed_quantity: 10,
            executed_price: 100,
            status: 'FILLED',
            created_at: '2026-05-22T13:00:00Z',
            filled_at: '2026-05-22T13:00:05Z',
            source: 'broker',
            cancellable: false,
          },
        ],
        total: 1,
        page: 1,
        page_size: 10,
        scope: 'today',
      },
    }).as('todayOrders')
    cy.intercept('PUT', '/api/trade-notes/42', {
      body: {
        id: 1,
        order_id: 42,
        symbol: 'AAPL.US',
        note: 'good entry',
        tags: ['momentum'],
        rating: 4,
        created_at: '2026-05-22T13:01:00Z',
        updated_at: '2026-05-22T13:01:00Z',
      },
    }).as('saveTradeNote')

    cy.visit('/#/history')
    cy.wait('@todayOrders')

    cy.contains('button', '＋ 添加').click()
    cy.get('[data-testid="trade-note-dialog"]').should('be.visible')
    cy.get('[data-testid="trade-note-input"]').type('good entry')
    cy.get('[data-testid="trade-note-save"]').click()
    cy.wait('@saveTradeNote')
    cy.contains('笔记已保存').should('be.visible')
  })

  it('renders read-only trade analytics panels', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/trades/analytics/calendar*', {
      body: {
        items: [
          { date: '2026-06-15', trade_count: 2, win_count: 1, loss_count: 1, net_pnl: 128.5, gross_pnl: 140, symbols: ['AAPL.US', 'MSFT.US'] },
        ],
        total_trades: 2,
        total_net_pnl: 128.5,
      },
    }).as('tradeCalendar')
    cy.intercept('GET', '/api/trades/analytics/hold-duration*', {
      body: {
        items: [
          { bucket: '5m-1h', min_seconds: 300, max_seconds: 3600, trade_count: 0, win_count: 0, loss_count: 0, win_rate: 0, net_pnl: 999, avg_net_pnl: 0 },
          { bucket: '<5m', min_seconds: null, max_seconds: 300, trade_count: 1, win_count: 1, loss_count: 0, win_rate: 100, net_pnl: 40, avg_net_pnl: 40 },
          { bucket: '1h-1d', min_seconds: 3600, max_seconds: 86400, trade_count: 1, win_count: 0, loss_count: 1, win_rate: 0, net_pnl: -12, avg_net_pnl: -12 },
        ],
        total_trades: 2,
      },
    }).as('holdDuration')
    cy.intercept('GET', '/api/trades/analytics/pnl-distribution*', {
      body: {
        items: [
          { bucket: '<-200', min_pnl: null, max_pnl: -200, trade_count: 0, net_pnl: -999 },
          { bucket: '-50-0', min_pnl: -50, max_pnl: 0, trade_count: 1, net_pnl: -12 },
          { bucket: '0-200', min_pnl: 0, max_pnl: 200, trade_count: 2, net_pnl: 188 },
        ],
        total_trades: 3,
        total_net_pnl: 176,
      },
    }).as('pnlDistribution')
    cy.intercept('GET', '/api/trades/analytics/monthly*', {
      body: {
        items: [
          { month: '2026-06', trade_count: 3, win_count: 2, loss_count: 1, win_rate: 66.6667, net_pnl: 50, gross_pnl: 64, cumulative_pnl: 176, drawdown: 12 },
        ],
        total_trades: 3,
        total_net_pnl: 176,
      },
    }).as('monthlySummary')
    cy.intercept('GET', '/api/trades/analytics/weekday*', {
      body: {
        items: [
          { weekday: 0, label: 'Mon', trade_count: 2, win_count: 1, loss_count: 1, win_rate: 50, net_pnl: 128.5, avg_net_pnl: 64.25 },
        ],
        total_trades: 2,
        total_net_pnl: 128.5,
      },
    }).as('weekdayAttribution')

    cy.visit('/#/history')
    cy.get('@tradeCalendar.all').should('have.length', 0)
    cy.get('@holdDuration.all').should('have.length', 0)
    cy.contains('交易分析（只读）').click()
    cy.wait(['@tradeCalendar', '@holdDuration', '@pnlDistribution', '@monthlySummary', '@weekdayAttribution'])

    cy.get('[data-testid="trade-analytics-section"]').should('be.visible')
    cy.get('[data-testid="trade-analytics-calendar-card"]').should('contain', '2026-06-15').and('contain', '+128.50')
    cy.get('[data-testid="trade-analytics-hold-duration-card"]').should('contain', '<5m').and('contain', '100.0%')
    cy.get('[data-testid="trade-analytics-pnl-distribution-card"]').should('contain', '0-200').and('contain', '+188.00')
    cy.get('[data-testid="trade-analytics-monthly-card"]').should('contain', '2026-06').and('contain', '回撤 12.00')
    cy.get('[data-testid="trade-analytics-weekday-card"]').should('contain', 'Mon').and('contain', '+128.50')
    cy.get('[data-testid="trade-analytics-insights"]')
      .should('contain', '最佳日 2026-06-15 +128.50')
      .and('contain', '最差日 2026-06-15 +128.50')
      .and('contain', '最活跃日 2026-06-15 2笔')
      .and('contain', '最佳持仓 <5m +40.00')
      .and('contain', '最差持仓 1h-1d -12.00')
      .and('contain', '亏损桶 1')
      .and('contain', '盈利桶 1')
      .and('contain', '分布净盈亏 +176.00')
      .and('contain', '最新月 2026-06 +50.00')
      .and('contain', '最佳月 2026-06 +50.00')
      .and('contain', '最大回撤月 2026-06 12.00')
      .and('contain', '最佳星期 Mon +128.50')
      .and('contain', '最差星期 Mon +128.50')
  })
})
