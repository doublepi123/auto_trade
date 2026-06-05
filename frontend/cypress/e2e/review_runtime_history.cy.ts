describe('Review runtime history', () => {
  it('loads symbol-scoped runtime charts for the selected symbol', () => {
    cy.intercept('GET', '/api/review*', {
      body: {
        symbol: 'AAPL.US',
        from_date: '2026-05-22',
        to_date: '2026-05-22',
        days: [
          {
            date: '2026-05-22',
            symbol: 'AAPL.US',
            llm_interactions: [],
            orders: [],
            events: [],
            snapshots: [],
            daily_pnl: 8.5,
            trade_count: 1,
            error_tags: [],
          },
        ],
        total_pnl: 8.5,
        total_trades: 1,
        all_error_tags: [],
      },
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', (req) => {
      expect(req.query.symbol).to.eq('AAPL.US')
      req.reply({
        body: {
          points: [
            {
              symbol: 'AAPL.US',
              timestamp: '2026-05-22T10:00:00Z',
              engine_state: 'flat',
              paused: false,
              kill_switch: false,
              daily_pnl: 0,
              consecutive_losses: 0,
              last_price: 198.5,
              last_trigger_price: 0,
            },
            {
              symbol: 'AAPL.US',
              timestamp: '2026-05-22T10:01:00Z',
              engine_state: 'long',
              paused: false,
              kill_switch: false,
              daily_pnl: 8.5,
              consecutive_losses: 0,
              last_price: 199.2,
              last_trigger_price: 198.9,
            },
          ],
          markers: [
            {
              timestamp: '2026-05-22T10:01:00Z',
              broker_order_id: 'filled-aapl',
              symbol: 'AAPL.US',
              side: 'BUY',
              quantity: 3,
              price: 199.1,
              status: 'FILLED',
            },
          ],
        },
      })
    }).as('getSymbolHistory')

    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: {
          last_push_age_seconds: 1,
          last_quote_age_seconds: 1,
          recent_quote_count: 2,
        },
        risk: {
          paused: false,
          kill_switch: false,
          pause_reason: '',
          daily_pnl: 8.5,
          consecutive_losses: 0,
        },
        symbol_runtimes: [
          {
            symbol: 'AAPL.US',
            market: 'US',
            is_primary: false,
            engine_state: 'long',
            last_price: 199.2,
            last_trigger_price: 198.9,
            recent_quote_count: 2,
            has_pending_order: false,
          },
        ],
      },
    }).as('getDiagnosticsForHistory')

    cy.setupApp()
    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').clear().type('AAPL.US')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-05-22').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-05-22').trigger('input').trigger('change')
    cy.contains('button', '查询').click()
    cy.wait('@getReview')
    cy.wait('@getSymbolHistory')
    cy.wait('@getDiagnosticsForHistory')
    cy.get('[data-testid="price-chart"]').should('be.visible')
    cy.get('[data-testid="pnl-chart"]').should('be.visible')
    cy.contains('AAPL.US · 2 个样本').should('be.visible')
  })

  it('shows diagnostics for the queried symbol', () => {
    cy.intercept('GET', '/api/review*', {
      body: {
        symbol: 'AAPL.US',
        from_date: '2026-05-22',
        to_date: '2026-05-22',
        days: [
          {
            date: '2026-05-22',
            symbol: 'AAPL.US',
            llm_interactions: [],
            orders: [],
            events: [],
            snapshots: [],
            daily_pnl: 0,
            trade_count: 0,
            error_tags: [],
          },
        ],
        total_pnl: 0,
        total_trades: 0,
        all_error_tags: [],
      },
    }).as('getReviewDiagnostics')

    cy.intercept('GET', '/api/status/history*', {
      body: {
        points: [],
        markers: [],
      },
    }).as('getReviewHistoryDiagnostics')

    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: ['AAPL.US'],
        quote_stream: {
          last_push_age_seconds: 2,
          last_quote_age_seconds: 1,
          recent_quote_count: 4,
        },
        risk: {
          paused: false,
          kill_switch: false,
          pause_reason: '',
          daily_pnl: 0,
          consecutive_losses: 0,
        },
        symbol_runtimes: [
          {
            symbol: 'AAPL.US',
            market: 'US',
            is_primary: false,
            engine_state: 'long',
            last_price: 199.5,
            last_trigger_price: 198.9,
            recent_quote_count: 3,
            has_pending_order: true,
          },
        ],
      },
    }).as('getDiagnostics')

    cy.setupApp()
    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').clear().type('AAPL.US')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-05-22').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-05-22').trigger('input').trigger('change')
    cy.contains('button', '查询').click()
    cy.wait('@getReviewDiagnostics')
    cy.wait('@getReviewHistoryDiagnostics')
    cy.wait('@getDiagnostics')
    cy.contains('运行诊断快照').should('be.visible')
    cy.contains('AAPL.US · long').should('be.visible')
    cy.contains('存在挂单').should('be.visible')
    cy.contains('最近推送 2.0s').should('be.visible')
  })
})
