describe('Notification Center', () => {
  it('lists dispatched notifications', () => {
    cy.visitApp('/#/notifications')
    cy.contains('通知中心', { timeout: 10000 }).should('be.visible')
    cy.contains('风控熔断').should('be.visible')
    cy.contains('日报').should('be.visible')
  })
})
