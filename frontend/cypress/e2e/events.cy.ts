describe('Decision Timeline', () => {
  it('displays recent trade and LLM events', () => {
    cy.visitApp('/#/events')
    cy.get('h3', { timeout: 10000 }).should('contain', '决策时间线')
    cy.contains('LLM 分析').should('be.visible')
    cy.contains('NVDA.US').should('be.visible')
    cy.contains('区间测试').should('be.visible')
    cy.contains('expected profit 4.00 is below required minimum profit 5.00').should('be.visible')
  })

  it('shows and filters skipped order categories', () => {
    cy.visitApp('/#/events')
    cy.contains('成本不足').should('be.visible')
    cy.get('[data-testid="skip-category-filter"]').click()
    cy.contains('.el-select-dropdown__item', '成本不足').click()
    cy.contains('订单跳过').should('be.visible')
    cy.contains('LLM 分析').should('not.exist')
  })
})
