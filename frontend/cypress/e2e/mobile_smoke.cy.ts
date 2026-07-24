describe('Mobile smoke tests', { viewportWidth: 390, viewportHeight: 844 }, () => {
  beforeEach(() => {
    cy.viewport(390, 844)
    cy.stubApi()
    cy.visit('/')
  })

  it('Dashboard loads without horizontal overflow', () => {
    cy.get('body').then(($body) => {
      expect($body[0].scrollWidth).to.be.lte($body[0].clientWidth)
    })
    cy.get('[data-testid="metrics-panel"]').then(($panel) => {
      const bounds = $panel[0].getBoundingClientRect()
      const viewportWidth = $panel[0].ownerDocument.defaultView?.innerWidth
      expect(viewportWidth).to.be.a('number')
      expect(bounds.left).to.be.gte(0)
      expect(bounds.right).to.be.lte(viewportWidth as number)
    })
  })

  it('Kill Switch button is visible and clickable', () => {
    cy.get('[data-testid="quick-actions"]').contains('button', /^紧急停止$/).should('be.visible').click()
    cy.contains('.el-message-box', '确定要触发紧急停止吗？').should('be.visible')
    cy.get('.el-message-box').find('.el-button--primary').click()
    cy.contains('紧急停止已触发').should('be.visible')
  })

  it('Navigation switches to bottom tabs on mobile', () => {
    cy.get('[data-testid="bottom-nav"]').should('be.visible')
    cy.get('[data-testid="desktop-nav"]').should('not.exist')
  })

  it('Bottom nav can switch to Strategy page', () => {
    cy.get('[data-testid="bottom-nav"]').contains('策略').click()
    cy.url().should('include', '/strategy')
    cy.contains('策略配置').should('be.visible')
  })

  it('Charts are collapsed by default on mobile', () => {
    cy.contains('展开图表').should('be.visible')
    cy.get('[data-testid="dashboard-charts"] .chart-panels').should('not.be.visible')
  })

  it('Can expand charts on mobile', () => {
    cy.contains('展开图表').click()
    cy.contains('收起图表').should('be.visible')
    cy.get('[data-testid="dashboard-charts"] .chart-panels').should('be.visible')
  })

  it('Strategy page loads without horizontal overflow', () => {
    cy.visit('/#/strategy')
    cy.get('body').then(($body) => {
      expect($body[0].scrollWidth).to.be.lte($body[0].clientWidth)
    })
  })

  it('Credentials page loads without horizontal overflow', () => {
    cy.visit('/#/credentials')
    cy.get('body').then(($body) => {
      expect($body[0].scrollWidth).to.be.lte($body[0].clientWidth)
    })
  })
})
