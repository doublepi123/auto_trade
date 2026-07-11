describe('Strategy status refresh', () => {
  it('refreshes LLM interval status after saving strategy changes', () => {
    let intervalMinutes = 2

    cy.intercept('GET', '/api/strategy', {
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
        llm_interval_minutes: intervalMinutes,
        fee_rate_us: 0.0005,
        fee_rate_hk: 0.003,
        min_repricing_pct: 0.003,
        llm_action_cooldown_seconds: 60,
        updated_at: '2026-01-01T00:00:00Z',
      },
    }).as('getStrategy')

    cy.intercept('GET', '/api/status', {
      body: {
        engine_state: 'flat',
        paused: false,
        kill_switch: false,
        runner_running: true,
        daily_pnl: 0,
        consecutive_losses: 0,
        last_price: 0,
        last_trigger_price: 0,
        last_trigger_at: null,
      },
    }).as('getStatus')

    cy.intercept('GET', '/api/strategy/llm-interval/status', (req) => {
      req.reply({
        body: {
          enabled: true,
          interval_minutes: intervalMinutes,
          last_analysis_at: '2026-05-19T19:52:03.545862Z',
          next_analysis_at: '2026-05-19T23:52:03.545862Z',
          current_suggestion: null,
          applied_values: null,
          reject_reason: null,
        },
      })
    }).as('getLLMStatus')

    cy.intercept('PUT', '/api/strategy', (req) => {
      expect(req.body).to.deep.equal({ llm_interval_minutes: 3 })
      intervalMinutes = req.body.llm_interval_minutes
      req.reply({
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
          llm_interval_minutes: intervalMinutes,
          updated_at: '2026-01-01T00:00:00Z',
        },
      })
    }).as('saveStrategy')

    cy.visit('/#/strategy')
    cy.contains('刷新间隔：2 分钟').should('be.visible')
    cy.contains('.el-form-item', 'LLM刷新间隔（分钟）')
      .find('input')
      .clear()
      .type('3')
      .blur()
    cy.contains('button', '保存').should('not.be.disabled').click()
    cy.wait('@saveStrategy')
    cy.contains('刷新间隔：3 分钟').should('be.visible')
  })
})
