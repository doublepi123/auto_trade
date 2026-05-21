describe('Credentials', () => {
  beforeEach(() => {
    cy.visitApp('/#/credentials')
    cy.get('h3', { timeout: 10000 }).should('contain', '凭证设置')
  })

  it('displays credentials form title', () => {
    cy.get('h3').should('contain', '凭证设置')
  })

  it('shows credential input labels', () => {
    cy.get('.el-form-item__label').should('contain', '长桥应用标识')
    cy.get('.el-form-item__label').should('contain', '长桥应用密钥')
    cy.get('.el-form-item__label').should('contain', '长桥访问令牌')
  })

  it('has save button', () => {
    cy.get('button.el-button--primary').should('be.visible')
  })

  it('saves entered credential fields', () => {
    cy.get('input').first().type('qa-app-key')
    cy.contains('button', '保存').should('not.be.disabled').click()
    cy.wait('@saveCredentials')
    cy.contains('已保存').should('be.visible')
  })
})
