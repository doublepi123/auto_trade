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
})
