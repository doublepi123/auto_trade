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
    cy.get('[data-testid="trade-note-input"] textarea').type('good entry')
    cy.get('[data-testid="trade-note-save"]').click()
    cy.wait('@saveTradeNote')
    cy.contains('笔记已保存').should('be.visible')
  })
})
