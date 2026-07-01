describe('Copy-to-clipboard affordance', () => {
  it('renders a copy button next to broker order ids in trade history', () => {
    cy.visitApp('/#/history')
    cy.contains('今日订单', { timeout: 10000 }).should('be.visible')
    // Order number text is preserved and a copy button appears alongside it.
    cy.contains('order-1').should('be.visible')
    cy.get('[data-testid="order-copy"]').should('have.length.greaterThan', 0)
  })

  it('renders a copy button next to broker order ids in the decision timeline', () => {
    cy.stubApi()
    // Default stub events have no broker_order_id; supply one with an id.
    cy.intercept('GET', '/api/events*', {
      body: {
        items: [
          {
            id: 1,
            source: 'trade',
            event_type: 'ORDER_SUBMITTED',
            symbol: 'AAPL.US',
            broker_order_id: 'broker-99',
            side: 'BUY',
            status: 'SUCCESS',
            message: '订单已提交',
            payload: {},
            created_at: '2026-06-16T10:00:00Z',
          },
        ],
        total: 1,
        page: 1,
        page_size: 20,
      },
    }).as('eventsWithId')
    cy.visit('/#/events')
    cy.contains('决策时间线', { timeout: 10000 }).should('be.visible')
    cy.contains('broker-99').should('be.visible')
    cy.get('[data-testid="timeline-order-copy"]').should('exist')
  })
})
