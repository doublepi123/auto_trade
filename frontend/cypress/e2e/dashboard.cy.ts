describe('Dashboard', () => {
  beforeEach(() => {
    cy.visitApp('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
  })

  it('displays engine state card', () => {
    cy.contains('引擎状态').should('be.visible')
  })

  it('displays price card', () => {
    cy.contains('最新价格').should('be.visible')
  })

  it('displays PnL card', () => {
    cy.contains('今日盈亏').should('be.visible')
  })

  it('displays total assets card', () => {
    cy.contains('总资产').should('be.visible')
  })

  it('displays cash balance card', () => {
    cy.contains('现金余额').should('be.visible')
  })

  it('displays positions card', () => {
    cy.contains('持仓明细').should('be.visible')
  })

  it('shows strategy info section', () => {
    cy.contains('行情信息').should('be.visible')
  })

  it('displays control buttons', () => {
    cy.contains('操作控制').should('be.visible')
    cy.get('button').contains('启动').should('be.visible')
    cy.get('button').contains('停止').should('be.visible')
  })

  it('displays risk status section', () => {
    cy.contains('风控状态').should('be.visible')
    cy.contains('紧急停止').should('be.visible')
    cy.contains('暂停状态').should('be.visible')
  })
})
