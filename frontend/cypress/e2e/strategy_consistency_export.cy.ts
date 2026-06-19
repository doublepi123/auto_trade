describe('Strategy LLM Consistency, Range Readout & Interaction Export', () => {
  function withStrategy(buyLow: number, sellHigh: number) {
    cy.stubApi()
    cy.intercept('GET', '/api/strategy', {
      body: {
        id: 1, symbol: 'AAPL.US', market: 'US', buy_low: buyLow, sell_high: sellHigh,
        short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
        min_profit_amount: 5, auto_resume_minutes: 3, llm_interval_minutes: 2,
        fee_rate_us: 0.0005, fee_rate_hk: 0.003, min_repricing_pct: 0.003,
        llm_action_cooldown_seconds: 60, trading_session_mode: 'ANY',
        updated_at: '2026-01-01T00:00:00Z',
      },
    }).as('getStrategy')
    cy.visit('/#/strategy')
    cy.contains('策略配置', { timeout: 10000 }).should('be.visible')
    cy.wait('@getStrategy')
    cy.wait('@getLLMIntervalStatus')
  }

  it('surfaces a consistency hint comparing suggestion vs applied vs form', () => {
    withStrategy(0, 0)
    cy.get('[data-testid="llm-consistency-hint"]').should('be.visible')
    // applied (220.42) differs from form (0) → config drift hint
    cy.get('[data-testid="llm-consistency-hint"]').should('contain', '配置已偏离')
  })

  it('computes a range gross-margin readout once buy_low/sell_high are set', () => {
    withStrategy(100, 200)
    cy.get('[data-testid="range-readout"]').should('be.visible')
    cy.get('[data-testid="range-readout"]').should('contain', '价差')
    cy.get('[data-testid="range-readout"]').should('contain', '净利')
  })

  it('exports recent LLM interactions as CSV and shows success rate', () => {
    withStrategy(100, 200)
    cy.get('[data-testid="llm-interaction-summary"]').should('contain', '成功率')
    cy.get('[data-testid="llm-interactions-export"]').click()
    cy.document().its('body').should('contain', '已导出')
  })
})
