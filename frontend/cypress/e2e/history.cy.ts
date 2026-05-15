describe('History', () => {
  beforeEach(() => {
    cy.visit('/#/history')
    cy.get('h3', { timeout: 10000 }).should('contain', '交易历史')
  })

  it('displays trade history page', () => {
    cy.get('h3').should('contain', '交易历史')
  })

  it('has refresh button', () => {
    cy.contains('button', '刷新').should('be.visible')
  })
})