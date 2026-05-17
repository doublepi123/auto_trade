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
})
