describe('Strategy LLM Card', () => {
  beforeEach(() => {
    cy.visit('/strategy')
    cy.wait(500)
  })

  it('displays LLM intelligent interval card', () => {
    cy.contains('LLM 智能区间').should('be.visible')
    cy.contains('启用').should('be.visible')
    cy.contains('禁用').should('be.visible')
  })

  it('has toggle switch for auto interval', () => {
    cy.get('.el-switch').first().should('exist')
  })

  it('has manual analyze button', () => {
    cy.contains('立即重新分析').should('be.visible')
  })
})
