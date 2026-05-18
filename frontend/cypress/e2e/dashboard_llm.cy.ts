describe('Dashboard LLM Indicator', () => {
  beforeEach(() => {
    cy.visit('/')
    cy.wait(500)
  })

  it('displays LLM status indicator when enabled', () => {
    cy.contains('LLM 智能区间').should('be.visible')
  })

  it('shows status tag', () => {
    cy.get('.el-tag').contains(/已启用|已禁用/).should('exist')
  })
})
