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
