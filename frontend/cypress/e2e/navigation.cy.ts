describe('Navigation', () => {
  it('navigates between pages via menu', () => {
    cy.visitApp('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')

    cy.get('.app-menu').contains('回测').click()
    cy.url().should('include', '/backtest')
    cy.contains('回测').should('be.visible')

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

    cy.get('.app-menu').contains('观察列表').click()
    cy.url().should('include', '/watchlist')
    cy.contains('观察列表').should('be.visible')

    cy.get('.app-menu').contains('复盘').click()
    cy.url().should('include', '/review')
    cy.contains('复盘工作台').should('be.visible')

    cy.get('.app-menu').contains('策略实验').click()
    cy.url().should('include', '/experiments')
    cy.contains('策略实验').should('be.visible')

    cy.get('.app-menu').contains('优化工作台').click()
    cy.url().should('include', '/lab')
    cy.contains('LLM 优化工作台').should('be.visible')

    cy.get('.app-menu').contains('仪表盘').click()
    cy.url().should('include', '/')
    cy.contains('仪表盘').should('be.visible')
  })
})
