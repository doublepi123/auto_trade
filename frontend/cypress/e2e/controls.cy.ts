describe('Controls', () => {
  beforeEach(() => {
    cy.visitApp('/')
    cy.contains('操作控制', { timeout: 10000 }).should('be.visible')
  })

  it('can pause trading', () => {
    cy.get('button').contains('暂停').click({ force: true })
    cy.wait('@pauseAction')
    cy.contains('已暂停').should('be.visible')
  })

  it('can stop and start the runner', () => {
    cy.get('button').contains('停止').click({ force: true })
    cy.wait('@stopAction')
    cy.contains('已暂停').should('be.visible')

    cy.get('button').contains('启动').click({ force: true })
    cy.wait('@startAction')
    cy.contains('运行中').should('be.visible')
  })

  it('can resume trading after pause', () => {
    cy.get('button').contains('暂停').click({ force: true })
    cy.wait('@pauseAction')
    cy.contains('已暂停').should('be.visible')
    cy.get('button').contains('恢复').click({ force: true })
    cy.wait('@resumeAction')
    cy.contains('运行中').should('be.visible')
  })

  it('shows emergency stop button', () => {
    cy.get('button').contains('紧急停止').should('be.visible')
  })

  it('can activate and disable emergency stop', () => {
    cy.get('button').contains('紧急停止').click({ force: true })
    cy.contains('button', /OK|确定/).click()
    cy.wait('@killSwitchAction')
    cy.contains('解除紧急停止').should('be.visible')

    cy.get('button').contains('解除紧急停止').click({ force: true })
    cy.wait('@disableKillSwitchAction')
    cy.contains('已关闭').should('be.visible')
  })
})
