describe('Dashboard LLM Indicator', () => {
  beforeEach(() => {
    cy.visitApp('/')
  })

  it('displays LLM status indicator when enabled', () => {
    cy.contains('LLM 智能区间').should('be.visible')
  })

  it('shows status tag', () => {
    cy.get('.el-tag').contains(/已启用|已禁用/).should('exist')
  })

  it('shows latest LLM refresh time', () => {
    cy.contains('最近刷新').should('be.visible')
    cy.contains('2026').should('be.visible')
  })

  it('displays LLM budget counters', () => {
    cy.get('[data-testid="llm-budget-bar"]').should('be.visible')
    cy.get('[data-testid="llm-budget-tracked"]').should('contain', '2/5')
    cy.get('[data-testid="llm-budget-hourly"]').should('contain', '12/60')
    cy.get('[data-testid="llm-budget-remaining"]').should('contain', '48')
  })

  it('displays per-symbol LLM schedule status', () => {
    cy.get('[data-testid="llm-symbol-status-table"]').should('be.visible')
    cy.get('[data-testid="llm-symbol-status-table"]').should('contain', 'AAPL.US')
    cy.get('[data-testid="llm-symbol-status-table"]').should('contain', 'NVDA.US')
    cy.contains('主标的').should('be.visible')
    cy.contains('观察').should('be.visible')
    cy.contains('45s').should('be.visible')
    cy.contains('120s').should('be.visible')
    cy.get('[data-testid="llm-symbol-status-table"]').should('contain', '同方向冷却中')
  })

  it('refreshes and displays latest LLM analysis details on the dashboard', () => {
    let calls = 0
    cy.intercept('GET', '/api/strategy/llm-interval/status', (req) => {
      calls += 1
      req.reply({
        body: {
          enabled: true,
          interval_minutes: 1,
          last_analysis_at: calls === 1 ? '2026-05-19T19:52:03.545862Z' : '2026-05-19T19:53:03.545862Z',
          next_analysis_at: calls === 1 ? '2026-05-19T19:53:03.545862Z' : '2026-05-19T19:54:03.545862Z',
          current_suggestion: {
            buy_low: calls === 1 ? 220.42 : 221.01,
            sell_high: calls === 1 ? 221.42 : 222.01,
            confidence_score: calls === 1 ? 0.75 : 0.81,
            analysis: calls === 1 ? '旧分析' : '新分析',
          },
          applied_values: { buy_low: 221.01, sell_high: 222.01 },
          reject_reason: null,
          budget: {
            max_symbols_per_cycle: 5,
            max_analyses_per_hour: 60,
            tracked_symbol_count: 2,
            effective_symbol_budget: 2,
            used_analyses_last_hour: calls === 1 ? 12 : 13,
            remaining_analyses_this_hour: calls === 1 ? 48 : 47,
          },
          symbol_statuses: [
            {
              symbol: 'AAPL.US',
              market: 'US',
              is_primary: true,
              has_pending_order: false,
              buy_cooldown_remaining_seconds: 0,
              sell_cooldown_remaining_seconds: calls === 1 ? 45 : 30,
              last_analysis_at: '2026-05-19T19:52:03.545862Z',
              next_analysis_at: '2026-05-19T19:53:03.545862Z',
              last_status: 'COOLDOWN',
              last_skip_reason: '同方向冷却中',
            },
            {
              symbol: 'NVDA.US',
              market: 'US',
              is_primary: false,
              has_pending_order: true,
              buy_cooldown_remaining_seconds: calls === 1 ? 120 : 90,
              sell_cooldown_remaining_seconds: 0,
              last_analysis_at: '2026-05-19T19:51:03.545862Z',
              next_analysis_at: '2026-05-19T19:54:03.545862Z',
              last_status: 'PENDING_ORDER',
              last_skip_reason: null,
            },
          ],
        },
      })
    }).as('refreshLLMIntervalStatus')

    cy.visit('/')
    cy.wait('@refreshLLMIntervalStatus')
    cy.get('[data-testid="llm-panel"]').should('contain.text', '分析')

    cy.wait('@refreshLLMIntervalStatus', { timeout: 5000 })
    cy.contains('新分析').should('be.visible')
    cy.contains('建议区间').should('be.visible')
    cy.contains('已应用').should('be.visible')
  })
})
