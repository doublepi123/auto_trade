describe('Review runtime history', () => {
  it('shows day composition and row-level review details', () => {
    cy.intercept('GET', '/api/review*', {
      body: {
        symbol: 'AAPL.US',
        from_date: '2026-05-23',
        to_date: '2026-05-23',
        days: [
          {
            date: '2026-05-23',
            symbol: 'AAPL.US',
            llm_interactions: [
              {
                id: 11,
                interaction_type: 'analyze',
                symbol: 'AAPL.US',
                market: 'US',
                success: true,
                order_action: 'BUY',
                order_status: 'FILLED',
                order_id: 'lb-review-1',
                applied: true,
                created_at: '2026-05-23T10:00:00Z',
              },
            ],
            orders: [
              {
                id: 21,
                broker_order_id: 'lb-review-1',
                symbol: 'AAPL.US',
                side: 'BUY',
                quantity: 3,
                price: 198.5,
                executed_quantity: 2,
                executed_price: 199.12,
                status: 'FILLED',
                created_at: '2026-05-23T10:01:00Z',
                filled_at: '2026-05-23T10:02:00Z',
              },
              {
                id: 22,
                broker_order_id: 'lb-review-cancelled',
                symbol: 'AAPL.US',
                side: 'SELL',
                quantity: 1,
                price: 210,
                executed_quantity: null,
                executed_price: null,
                status: 'CANCELLED',
                created_at: '2026-05-23T10:04:00Z',
                filled_at: null,
              },
            ],
            events: [
              {
                id: 31,
                event_type: 'ORDER_FILLED',
                symbol: 'AAPL.US',
                broker_order_id: 'lb-review-1',
                side: 'BUY',
                status: 'FILLED',
                message: 'order filled',
                payload_json: '{"skip_category":"FEE","expected_profit":12.34}',
                created_at: '2026-05-23T10:02:30Z',
              },
            ],
            snapshots: [
              {
                id: 41,
                engine_state: 'long',
                daily_pnl: -12.5,
                consecutive_losses: 2,
                last_price: 199.5,
                last_trigger_price: 198.9,
                created_at: '2026-05-23T10:03:00Z',
              },
            ],
            daily_pnl: -12.5,
            trade_count: 1,
            error_tags: ['FEE'],
          },
        ],
        total_pnl: -12.5,
        total_trades: 1,
        all_error_tags: ['FEE'],
      },
    }).as('getReviewDetails')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getReviewDetailsHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 1 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: -12.5, consecutive_losses: 2 },
        symbol_runtimes: [],
      },
    }).as('getReviewDetailsDiagnostics')

    cy.setupApp()
    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').clear().type('AAPL.US')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-05-23').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-05-23').trigger('input').trigger('change')
    cy.contains('button', '查询').click()
    cy.wait('@getReviewDetails')
    cy.wait('@getReviewDetailsHistory')
    cy.wait('@getReviewDetailsDiagnostics')

    cy.get('[data-testid="review-day-composition"]').should('contain', 'LLM 1').and('contain', '订单 2').and('contain', '事件 1').and('contain', '快照 1').and('contain', '错误 1')
    cy.get('[data-testid="review-day-state"]').should('contain', '亏损').and('contain', '有交易').and('contain', '有错误')
    cy.contains('[data-testid="review-order-detail"]', 'lb-review-1').should('contain', '成交 2').and('contain', '成交价').and('contain', '199.12').and('contain', '成交时间')
    cy.contains('[data-testid="review-order-detail"]', 'lb-review-cancelled').should('contain', '委托 1 股').and('contain', '210.00').and('contain', '成交 -').and('contain', '成交价 -').and('contain', '成交时间 -')
    cy.get('[data-testid="review-event-payload"]').should('contain', 'skip_category').and('contain', 'expected_profit')
    cy.get('[data-testid="review-snapshot-detail"]').should('contain', '触发价').and('contain', '+0.60').and('contain', '连亏 2')
  })

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
