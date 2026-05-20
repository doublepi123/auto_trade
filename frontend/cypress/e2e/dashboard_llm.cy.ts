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
})
