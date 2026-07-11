describe('P0 live safety controls', () => {
  beforeEach(() => {
    cy.stubApi()
  })

  it('shows bounded strategy controls with unsafe toggles locked off', () => {
    cy.intercept('GET', '/api/strategy/llm-interval/status', {
      body: {
        enabled: true,
        shadow_mode: true,
        policy_status: 'SHADOW',
        interval_minutes: 2,
        last_analysis_at: null,
        next_analysis_at: null,
        current_suggestion: null,
        applied_values: null,
        last_applied_values: { buy_low: 100, sell_high: 101 },
        reject_reason: null,
        budget: {
          max_symbols_per_cycle: 1,
          max_analyses_per_hour: 30,
          tracked_symbol_count: 1,
          effective_symbol_budget: 1,
          used_analyses_last_hour: 0,
          remaining_analyses_this_hour: 30,
        },
        symbol_statuses: [],
      },
    })
    cy.visit('/#/strategy')

    cy.get('[data-testid="llm-policy-mode"]').should('contain', '影子观察')
    cy.contains('历史实盘应用').should('be.visible')
    cy.get('[data-testid="allow-position-addons"]').should('be.visible')
    cy.get('[data-testid="allow-position-addons"]').should('have.class', 'is-disabled')
    cy.get('[data-testid="max-position-quantity"]').should('be.visible')
    cy.get('[data-testid="max-position-notional"]').should('be.visible')
    cy.get('[data-testid="max-risk-per-trade"]').should('be.visible')
    cy.get('[data-testid="stop-loss-pct"]').should('be.visible')
    cy.get('[data-testid="max-holding-minutes"]').should('be.visible')
    cy.get('[data-testid="entry-cutoff-minutes"]').should('be.visible')
    cy.get('[data-testid="flatten-minutes"]').should('be.visible')
    cy.get('[data-testid="llm-order-execution"]').should('have.class', 'is-disabled')
  })

  it('surfaces a persisted reducing state on the dashboard', () => {
    cy.intercept('GET', '/api/status', {
      body: {
        engine_state: 'long',
        paused: false,
        kill_switch: false,
        runner_running: true,
        daily_pnl: 0,
        consecutive_losses: 0,
        last_price: 98.8,
        last_trigger_price: 0,
        last_trigger_at: null,
        last_action_message: 'hard stop reached',
        trading_session_mode: 'RTH_ONLY',
        is_trading_hours: true,
        execution_state: 'REDUCING',
        reduction_reason: 'long hard stop reached',
        reduction_started_at: '2026-07-10T18:00:00Z',
      },
    })

    cy.visit('/#/')

    cy.contains('减仓中').should('be.visible')
    cy.contains('保护性退出处理中').should('be.visible')
  })
})
