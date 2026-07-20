describe('Lab Budget Progress & Runtime Export', () => {
  beforeEach(() => {
    cy.intercept('GET', '/api/llm-usage/summary*', {
      body: {
        days: 30,
        total_interactions: 0,
        successful_interactions: 0,
        total_prompt_tokens: 0,
        total_completion_tokens: 0,
        total_tokens: 0,
        by_day: [],
        by_type: [],
      },
    }).as('usageSummary')
    cy.visitApp('/#/lab')
  })

  it('shows budget usage progress bar at 100% when exhausted', () => {
    cy.intercept('GET', '/api/strategy/llm-interval/status', {
      body: {
        enabled: true,
        interval_minutes: 2,
        last_analysis_at: '2026-06-17T10:00:00Z',
        next_analysis_at: '2026-06-17T10:02:00Z',
        current_suggestion: { buy_low: 180.5, sell_high: 188.8, confidence_score: 0.8, analysis: '' },
        applied_values: null,
        reject_reason: null,
        budget: { max_symbols_per_cycle: 5, max_analyses_per_hour: 12, tracked_symbol_count: 3, effective_symbol_budget: 3, used_analyses_last_hour: 12, remaining_analyses_this_hour: 0 },
        symbol_statuses: [
          { symbol: 'AAPL.US', market: 'US', is_primary: true, has_pending_order: false, buy_cooldown_remaining_seconds: null, sell_cooldown_remaining_seconds: 30, last_analysis_at: '2026-06-17T10:00:00Z', next_analysis_at: '2026-06-17T10:02:00Z', last_status: 'SKIPPED', last_skip_reason: 'cooldown active' },
        ],
      },
    }).as('runtimeStatus')
    cy.intercept('GET', '/api/strategy/llm-interval/interactions*', {
      body: [
        { id: 7, interaction_type: 'analyze', symbol: 'AAPL.US', market: 'US', success: true, error: '', order_action: 'BUY', order_status: 'SUBMITTED', order_id: 'ord-7', applied: true, created_at: '2026-06-17T10:00:00Z' },
      ],
    }).as('runtimeInteractions')

    cy.contains('.el-tabs__item', '运行状态').click()
    cy.wait(['@runtimeStatus', '@runtimeInteractions'])

    cy.get('[data-testid="llm-budget-progress"]').should('be.visible')
    cy.get('[data-testid="llm-budget-progress"] .budget-bar-note').should('contain', '12 / 12')
    cy.get('[data-testid="llm-runtime-health"]').should('contain', '预算已耗尽')
  })

  it('exports symbol status and interactions CSV from runtime cards', () => {
    cy.intercept('GET', '/api/strategy/llm-interval/status', {
      body: {
        enabled: true, interval_minutes: 2, last_analysis_at: '2026-06-17T10:00:00Z', next_analysis_at: '2026-06-17T10:02:00Z',
        current_suggestion: null, applied_values: null, reject_reason: null,
        budget: { max_symbols_per_cycle: 5, max_analyses_per_hour: 60, tracked_symbol_count: 1, effective_symbol_budget: 1, used_analyses_last_hour: 12, remaining_analyses_this_hour: 48 },
        symbol_statuses: [{ symbol: 'AAPL.US', market: 'US', is_primary: true, has_pending_order: false, buy_cooldown_remaining_seconds: null, sell_cooldown_remaining_seconds: 0, last_analysis_at: '2026-06-17T10:00:00Z', next_analysis_at: '2026-06-17T10:02:00Z', last_status: 'APPLIED', last_skip_reason: null }],
      },
    }).as('runtimeStatus')
    cy.intercept('GET', '/api/strategy/llm-interval/interactions*', {
      body: [{ id: 7, interaction_type: 'analyze', symbol: 'AAPL.US', market: 'US', success: true, error: '', order_action: 'BUY', order_status: null, order_id: null, applied: true, created_at: '2026-06-17T10:00:00Z' }],
    }).as('runtimeInteractions')

    cy.contains('.el-tabs__item', '运行状态').click()
    cy.wait(['@runtimeStatus', '@runtimeInteractions'])

    cy.get('[data-testid="lab-export-symbols"]').should('not.be.disabled').click()
    cy.document().its('body').should('contain', '已导出 Symbol 状态')

    cy.get('[data-testid="lab-export-interactions"]').should('not.be.disabled').click()
    cy.document().its('body').should('contain', '已导出 1 条交互')
  })
})
