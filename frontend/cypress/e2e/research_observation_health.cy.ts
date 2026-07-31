describe('research observation health', () => {
  it('shows each deployed research observer and its health', () => {
    cy.stubApi()
    cy.visit('/#/strategy-health')

    cy.wait('@getObservationHealth')
    cy.get('[data-testid="observation-health-status"]').should('contain', '健康')
    cy.get('[data-testid="observation-health-table"]').within(() => {
      cy.contains('指数股票池刷新').should('be.visible')
      cy.contains('月末轮动预承诺').should('be.visible')
      cy.contains('量化评分覆盖').should('be.visible')
      cy.contains('分散优先观察').should('be.visible')
      cy.contains('成长卫星观察').should('be.visible')
      cy.contains('Live 区间对齐').should('be.visible')
      cy.contains('Live 退出挑战者').should('be.visible')
      cy.contains('Strategy v2 退出挑战者').should('be.visible')
      cy.contains('Strategy v2 前瞻回放').should('be.visible')
      cy.contains('应到会话').should('be.visible')
      cy.contains('组合路由观察').should('be.visible')
      cy.contains('开盘策略影子').should('be.visible')
      cy.contains('开盘模拟执行').should('be.visible')
    })
  })

  it('translates Strategy v2 forward blockers without hiding counts', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/universe/observation-health', {
      body: {
        generated_at: '2026-08-01T02:00:00Z',
        status: 'DEGRADED',
        order_submission_allowed: false,
        automatic_promotion_allowed: false,
        blockers: [
          'STRATEGY_V2_FORWARD:FORWARD_EVIDENCE_MISSING_AFTER_CLOSED_SESSION_3',
        ],
        components: [{
          name: 'STRATEGY_V2_FORWARD',
          status: 'DEGRADED',
          latest_at: '2026-07-31T01:49:38Z',
          age_seconds: 86400,
          latest_session_date: null,
          expected_session_date: '2026-07-31',
          observed_count: 44,
          expected_count: 44,
          coverage_ratio: 1,
          blockers: [
            'FORWARD_EVIDENCE_MISSING_AFTER_CLOSED_SESSION_3',
          ],
        }],
      },
    }).as('getDegradedObservationHealth')

    cy.visit('/#/strategy-health')
    cy.wait('@getDegradedObservationHealth')
    cy.get('[data-testid="observation-health-table"]')
      .should('contain', '完整交易日后仍缺前瞻证据 3 个标的')
    cy.get('.observation-alert')
      .should('contain', 'Strategy v2 前瞻回放：完整交易日后仍缺前瞻证据 3 个标的')
  })
})
