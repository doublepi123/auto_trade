describe('TradeHistory Fill Quality, Note Filter & Closed-trade Export', () => {
  beforeEach(() => {
    cy.stubApi()
    // Register the closed-trades override BEFORE visiting so it shadows the
    // support stub for the mount-time loadClosedTrades() request.
    cy.intercept({ method: 'GET', pathname: '/api/trades' }, {
      body: {
        items: [
          {
            symbol: 'AAPL.US', side: 'long', entry_order_id: 1, exit_order_id: 2,
            entry_at: '2026-01-01T10:01:00Z', exit_at: '2026-01-05T11:01:00Z',
            entry_price: 10, exit_price: 12, quantity: 100,
            gross_pnl: 200, est_fees: 2.2, net_pnl: 197.8, holding_seconds: 345600,
          },
        ],
        total: 1,
      },
    }).as('trades')
    cy.visit('/#/history')
    cy.wait('@getOrders')
    cy.wait('@getTradeNoteAnalytics')
  })

  it('shows a slippage tag for orders filled away from the quote', () => {
    // Support order: BUY price 150, executed_price 149.5 → favorable -0.50
    cy.get('[data-testid="order-slippage"]').should('have.length.at.least', 1)
    cy.get('[data-testid="order-slippage"]').first().should('contain', '-0.50')
  })

  it('filters orders to only those with notes when toggled', () => {
    cy.get('[data-testid="orders-only-notes"] input').check({ force: true })
    // Only order 1 has a note → AAPL.US still present
    cy.get('.orders-page .el-table').should('contain', 'AAPL.US')
  })

  it('exports the active closed-trade filters as backend CSV', () => {
    cy.intercept('GET', '/api/trades/export*', (req) => {
      expect(req.query.format).to.eq('csv')
      expect(req.query.symbol).to.eq('AAPL.US')
      expect(req.query.from_date).to.eq('2026-01-01')
      expect(req.query.to_date).to.eq('2026-01-31')
      req.reply({
        body: 'symbol,net_pnl\nAAPL.US,197.8\n',
        headers: { 'content-type': 'text/csv' },
      })
    }).as('exportClosedTrades')
    cy.contains('已实现成交（往返配对').click()
    cy.get('[data-testid="roundtrips-table"]').should('be.visible')
    cy.get('[data-testid="roundtrip-symbol-search"]').type('AAPL.US')
    cy.get('.roundtrips-controls .el-date-editor').eq(0).find('input').type('2026-01-01{enter}')
    cy.get('.roundtrips-controls .el-date-editor').eq(1).find('input').type('2026-01-31{enter}')
    cy.get('[data-testid="trades-export-csv"]').should('not.be.disabled').click()
    cy.wait('@exportClosedTrades')
  })
})
