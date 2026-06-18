describe('Closed round-trip trades', () => {
  it('lists paired entry<->exit round trips with net/gross PnL', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/trades*', {
      body: {
        items: [
          {
            symbol: 'AAPL.US',
            side: 'long',
            entry_order_id: 1,
            exit_order_id: 2,
            entry_at: '2026-01-01T10:01:00Z',
            exit_at: '2026-01-05T11:01:00Z',
            entry_price: 10,
            exit_price: 12,
            quantity: 100,
            gross_pnl: 200,
            est_fees: 2.2,
            net_pnl: 197.8,
            holding_seconds: 345600,
          },
        ],
        total: 1,
      },
    }).as('trades')

    cy.visit('/#/history')
    cy.wait('@trades')

    // Expand the collapsible round-trip panel.
    cy.contains('已实现成交（往返配对').click()
    cy.get('[data-testid="roundtrips-table"]', { timeout: 10000 }).should('be.visible')
    cy.contains('AAPL.US').should('be.visible')
    cy.contains('+197.80').should('be.visible')
    cy.contains('+200.00').should('be.visible')
  })

  it('shows empty state when no round trips are paired', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/trades*', { body: { items: [], total: 0 } }).as('trades')
    cy.visit('/#/history')
    cy.wait('@trades')
    cy.contains('已实现成交（往返配对').click()
    cy.get('[data-testid="roundtrips-table"]').should('be.visible')
    cy.contains('共 0 笔').should('be.visible')
    cy.get('[data-testid="roundtrips-table"]').should('contain', '暂无往返成交')
  })

  it('renders the trade-stats strip (win rate / streaks / expectancy)', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/trades*', { body: { items: [], total: 0 } }).as('trades')
    cy.intercept('GET', '/api/trades/stats*', {
      body: {
        total_trades: 4,
        win_count: 3,
        loss_count: 1,
        breakeven_count: 0,
        win_rate: 75.0,
        total_gross_pnl: 220,
        total_net_pnl: 210,
        avg_win: 100,
        avg_loss: 80,
        expectancy: 52.5,
        profit_factor: 3.75,
        payoff_ratio: 1.25,
        largest_win: 120,
        largest_loss: -80,
        current_streak_type: 'win',
        current_streak_count: 2,
        max_win_streak: 3,
        max_loss_streak: 1,
        avg_hold_seconds: 5400,
      },
    }).as('stats')
    cy.visit('/#/history')
    cy.wait('@stats')
    cy.contains('已实现成交（往返配对').click()
    cy.get('[data-testid="trade-stats"]').should('be.visible')
    cy.contains('75.0%').should('be.visible')
    cy.contains('2胜').should('be.visible')
  })

  it('shows round-trip filters, summary, insights, and expandable details', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/trades*', {
      body: {
        items: [
          {
            symbol: 'AAPL.US', side: 'long', entry_order_id: 11, exit_order_id: 12,
            entry_at: '2026-06-17T10:00:00Z', exit_at: '2026-06-17T11:00:00Z',
            entry_price: 100, exit_price: 103, quantity: 10,
            gross_pnl: 30, est_fees: 1.2, net_pnl: 28.8, holding_seconds: 3600,
          },
          {
            symbol: 'TSLA.US', side: 'short', entry_order_id: 21, exit_order_id: 22,
            entry_at: '2026-06-16T10:00:00Z', exit_at: '2026-06-16T10:30:00Z',
            entry_price: 210, exit_price: 214, quantity: 5,
            gross_pnl: -20, est_fees: 1.5, net_pnl: -21.5, holding_seconds: 1800,
          },
          {
            symbol: 'MSFT.US', side: 'long', entry_order_id: 31, exit_order_id: 32,
            entry_at: '2026-06-15T10:00:00Z', exit_at: '2026-06-15T10:10:00Z',
            entry_price: 300, exit_price: 300.5, quantity: 2,
            gross_pnl: 1, est_fees: 0.6, net_pnl: 0.4, holding_seconds: 600,
          },
        ],
        total: 5,
      },
    }).as('trades')

    cy.visit('/#/history')
    cy.wait('@trades')
    cy.contains('已实现成交（往返配对').click()

    cy.get('[data-testid="roundtrip-summary"]')
      .should('contain', '当前已加载 3 / 5')
      .and('contain', '当前筛选 3')
      .and('contain', '胜 2')
      .and('contain', '败 1')
      .and('contain', '+7.70')
    cy.get('[data-testid="roundtrip-insights"]').should('contain', '最佳 AAPL.US +28.80').and('contain', '最差 TSLA.US -21.50')
    cy.get('[data-testid="roundtrip-symbol-search"]').type('TSLA')
    cy.get('[data-testid="roundtrips-table"]').should('contain', 'TSLA.US').and('not.contain', 'AAPL.US')
    cy.get('[data-testid="roundtrip-filter-winners"]').click()
    cy.get('[data-testid="roundtrips-table"]').should('contain', '暂无匹配的往返成交')
    cy.get('[data-testid="roundtrip-filter-all"]').click()
    cy.get('[data-testid="roundtrip-symbol-search"]').clear()
    cy.get('[data-testid="roundtrip-filter-losers"]').click()
    cy.get('[data-testid="roundtrips-table"]').should('contain', 'TSLA.US').and('not.contain', 'AAPL.US')
    cy.get('[data-testid="roundtrip-filter-all"]').click()
    cy.get('.el-table__expand-icon').first().click()
    cy.get('[data-testid="roundtrip-detail"]').should('contain', 'entry #11').and('contain', 'exit #12').and('contain', '费用拖累 1.20')
  })
})
