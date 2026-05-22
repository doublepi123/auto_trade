describe('Decision Timeline', () => {
  it('displays recent trade and LLM events', () => {
    cy.visitApp('/#/events')
    cy.get('h3', { timeout: 10000 }).should('contain', '决策时间线')
    cy.contains('LLM 分析').should('be.visible')
    cy.contains('NVDA.US').should('be.visible')
    cy.contains('区间测试').should('be.visible')
  })
})
