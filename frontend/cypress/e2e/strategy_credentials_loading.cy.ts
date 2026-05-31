describe('Configuration loading guards', () => {
  beforeEach(() => {
    cy.stubApi()
  })

  it('does not expose editable strategy defaults before strategy loads', () => {
    cy.intercept('GET', '**/api/strategy', {
      delay: 1000,
      body: {
        id: 1,
        symbol: 'AAPL.US',
        market: 'US',
        buy_low: 100,
        sell_high: 200,
        short_selling: false,
        min_profit_amount: 0,
        auto_resume_minutes: 3,
        max_daily_loss: 5000,
        max_consecutive_losses: 3,
        llm_interval_minutes: 2,
        fee_rate_us: 0.0005,
        fee_rate_hk: 0.003,
        min_repricing_pct: 0.003,
        llm_action_cooldown_seconds: 60,
        trading_session_mode: 'ANY',
        margin_safety_factor: 0.9,
        updated_at: new Date().toISOString(),
      },
    })

    cy.visit('/#/strategy')
    cy.get('.el-card .el-loading-mask', { timeout: 10000 }).should('exist')
    cy.get('[data-testid="strategy-config-form"] input').first().should('be.disabled')
  })

  it('keeps credentials inputs disabled while credential state loads', () => {
    cy.intercept('GET', '**/api/credentials', {
      delay: 1000,
      body: {
        id: 1,
        longbridge_app_key: '',
        longbridge_app_secret: '',
        longbridge_access_token: '',
        sct_key: '',
        has_longbridge_app_key: true,
        has_longbridge_app_secret: true,
        has_longbridge_access_token: true,
        has_sct_key: false,
        notification_channels: [],
        updated_at: new Date().toISOString(),
      },
    })

    cy.visit('/#/credentials')
    cy.get('input').first().should('be.disabled')
  })
})
