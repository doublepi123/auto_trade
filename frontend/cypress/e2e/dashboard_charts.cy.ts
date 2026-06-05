describe('Dashboard Charts', () => {
  it('loads price and pnl charts with trade signal markers', () => {
    cy.visitApp('/')
    cy.get('[data-testid="price-chart"]', { timeout: 10000 }).should('be.visible')
    cy.get('[data-testid="pnl-chart"]').should('be.visible')
    cy.get('[data-testid="trade-signal-marker"]').should('exist')
    cy.contains('价格走势').should('be.visible')
    cy.contains('盈亏曲线').should('be.visible')
    cy.contains('交易信号').should('be.visible')
  })

  it('switches chart history to an observed symbol', () => {
    cy.setupApp()
    cy.stubApi()
    cy.intercept('GET', '/api/status/history*', (req) => {
      const symbol = typeof req.query.symbol === 'string' ? req.query.symbol : ''
      if (symbol === 'AAPL.US') {
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
              {
                symbol: 'AAPL.US',
                timestamp: '2026-05-22T10:02:00Z',
                engine_state: 'long',
                paused: false,
                kill_switch: false,
                daily_pnl: 12.2,
                consecutive_losses: 0,
                last_price: 200.1,
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
        return
      }

      req.reply({
        body: {
          points: [
            {
              symbol: 'NVDA.US',
              timestamp: '2026-05-22T10:00:00Z',
              engine_state: 'flat',
              paused: false,
              kill_switch: false,
              daily_pnl: 0,
              consecutive_losses: 0,
              last_price: 220.1,
              last_trigger_price: 0,
            },
            {
              symbol: 'NVDA.US',
              timestamp: '2026-05-22T10:01:00Z',
              engine_state: 'long',
              paused: false,
              kill_switch: false,
              daily_pnl: 12.5,
              consecutive_losses: 0,
              last_price: 221.2,
              last_trigger_price: 220.6,
            },
          ],
          markers: [
            {
              timestamp: '2026-05-22T10:01:00Z',
              broker_order_id: 'filled-1',
              symbol: 'NVDA.US',
              side: 'BUY',
              quantity: 3,
              price: 220.6,
              status: 'FILLED',
            },
          ],
        },
      })
    }).as('getStatusHistorySwitch')

    cy.visit('/#/')
    cy.get('[data-testid="chart-symbol-select"]').click()
    cy.get('.el-select-dropdown__item').contains('AAPL.US').click()
    cy.wait('@getStatusHistorySwitch')
    cy.get('[data-testid="chart-symbol-current"]').should('contain', 'AAPL.US')
    cy.get('[data-testid="price-chart"]').contains('3 个样本').should('be.visible')
    cy.get('[data-testid="pnl-chart"]').contains('3 个样本').should('be.visible')
  })
})
