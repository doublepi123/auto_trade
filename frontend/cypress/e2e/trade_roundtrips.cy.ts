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
})
