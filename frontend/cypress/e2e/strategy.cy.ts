describe('Strategy', () => {
  beforeEach(() => {
    cy.visitApp('/#/strategy')
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

  it('accepts cent-level decimal prices without native number validation errors', () => {
    cy.contains('.el-form-item', '买入价下限')
      .find('input')
      .should('have.attr', 'step', '0.01')
      .clear()
      .type('218.50')
      .then(($input) => {
        expect(($input[0] as HTMLInputElement).checkValidity()).to.equal(true)
      })

    cy.contains('.el-form-item', '卖出价上限')
      .find('input')
      .should('have.attr', 'step', '0.01')
      .clear()
      .type('219.50')
      .then(($input) => {
        expect(($input[0] as HTMLInputElement).checkValidity()).to.equal(true)
      })
  })
})
