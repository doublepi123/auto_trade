describe('Navigation', () => {
  it('navigates between pages via menu', () => {
    cy.visitApp('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')

    cy.get('.app-menu').contains('策略配置').click()
    cy.url().should('include', '/strategy')
    cy.contains('策略配置').should('be.visible')

    cy.get('.app-menu').contains('凭证设置').click()
    cy.url().should('include', '/credentials')
    cy.contains('凭证设置').should('be.visible')

    cy.get('.app-menu').contains('交易历史').click()
    cy.url().should('include', '/history')
    cy.contains('交易历史').should('be.visible')

    cy.get('.app-menu').contains('决策时间线').click()
    cy.url().should('include', '/events')
    cy.contains('决策时间线').should('be.visible')

    cy.get('.app-menu').contains('仪表盘').click()
    cy.url().should('include', '/')
    cy.contains('仪表盘').should('be.visible')
  })
})
