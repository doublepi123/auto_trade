describe('Strategy trading session mode', () => {
  it('saves RTH_ONLY and sends trading_session_mode in PUT', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/strategy', {
      body: {
        id: 1,
        symbol: 'AAPL.US',
        market: 'US',
        buy_low: 100,
        sell_high: 200,
        short_selling: false,
        max_daily_loss: 5000,
        max_consecutive_losses: 3,
        min_profit_amount: 0,
        auto_resume_minutes: 3,
        llm_interval_minutes: 2,
        fee_rate_us: 0.0005,
        fee_rate_hk: 0.003,
        min_repricing_pct: 0.003,
        llm_action_cooldown_seconds: 60,
        trading_session_mode: 'ANY',
        updated_at: '2026-01-01T00:00:00Z',
      },
    }).as('getStrategySession')

    cy.intercept('PUT', '/api/strategy', (req) => {
      expect(req.body.trading_session_mode).to.equal('RTH_ONLY')
      req.reply({
        body: {
          ...req.body,
          id: 1,
          updated_at: '2026-01-01T00:00:00Z',
        },
      })
    }).as('saveStrategySession')

    cy.visit('/#/strategy')
    cy.contains('策略配置', { timeout: 10000 }).should('be.visible')
    cy.wait('@getStrategySession')

    cy.get('[data-testid="trading-session-mode"]')
      .contains('.el-radio', '仅常规交易时段')
      .click()

    cy.get('[data-testid="strategy-save"]').should('not.be.disabled').click()
    cy.wait('@saveStrategySession')
  })
})
