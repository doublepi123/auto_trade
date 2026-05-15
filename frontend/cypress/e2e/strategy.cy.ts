describe('Strategy', () => {
  beforeEach(() => {
    cy.visit('/strategy')
    cy.contains('策略配置', { timeout: 10000 }).should('be.visible')
  })

  it('displays strategy form fields', () => {
    cy.contains('股票代码').should('be.visible')
    cy.contains('买入价下限').should('be.visible')
    cy.contains('市场').should('be.visible')
  })

  it('has save button', () => {
    cy.get('.el-button--primary').should('be.visible')
  })
})