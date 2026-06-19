describe('Decision Timeline Summary, Payload & Quick Filter', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.intercept('GET', '/api/events*', {
      body: {
        items: [
          { id: 1, source: 'trade', event_type: 'ORDER_FILLED', symbol: 'NVDA.US', broker_order_id: 'o1', side: 'BUY', status: 'FILLED', message: 'filled', payload: { qty: 2 }, created_at: '2026-06-16T12:00:00Z' },
          { id: 2, source: 'trade', event_type: 'ORDER_SKIPPED', symbol: 'NVDA.US', broker_order_id: '', side: 'SELL', status: 'SKIPPED', message: 'fee too low', payload: { skip_category: 'FEE', expected_profit: 4 }, created_at: '2026-06-16T12:01:00Z' },
          { id: 3, source: 'trade', event_type: 'ORDER_SKIPPED', symbol: 'AAPL.US', broker_order_id: '', side: 'SELL', status: 'SKIPPED', message: 'fee', payload: { skip_category: 'FEE' }, created_at: '2026-06-16T12:02:00Z' },
          { id: 4, source: 'trade', event_type: 'ORDER_REJECTED', symbol: 'AAPL.US', broker_order_id: 'o2', side: 'BUY', status: 'REJECTED', message: 'no balance', payload: {}, created_at: '2026-06-16T12:03:00Z' },
        ],
        total: 4, page: 1, page_size: 20,
      },
    }).as('getEvents')
    cy.visit('/#/events')
    cy.wait('@getEvents')
  })

  it('renders within-page summary chips with derived counts', () => {
    cy.get('[data-testid="timeline-summary"]').should('contain', '本页 4')
    cy.get('[data-testid="timeline-summary"]').should('contain', '跳过 2')
    cy.get('[data-testid="timeline-summary"]').should('contain', '成交 1')
    cy.get('[data-testid="timeline-summary"]').should('contain', '失败 1')
    cy.get('[data-testid="timeline-summary"]').should('contain', '主跳过 成本不足 (2)')
  })

  it('expands a row to reveal the full payload JSON', () => {
    cy.get('[data-testid="timeline-payload"]').should('not.exist')
    cy.get('.el-table__expand-icon').eq(1).click()
    cy.get('[data-testid="timeline-payload"]').should('contain', 'skip_category')
    cy.get('[data-testid="timeline-payload"]').should('contain', 'expected_profit')
  })

  it('quick-filters by a row symbol', () => {
    cy.get('[data-testid="timeline-filter-symbol"]').should('have.length', 4)
    cy.get('[data-testid="timeline-filter-symbol"]').first().click()
    cy.get('input[placeholder="搜索消息 / 标的 / 事件类型"]').should('have.value', 'NVDA.US')
  })
})
