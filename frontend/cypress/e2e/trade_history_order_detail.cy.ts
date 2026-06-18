describe('Trade History Order Detail Drawer', () => {
  beforeEach(() => {
    cy.visitApp('/#/history')
    cy.wait('@getOrders')
  })

  it('opens order detail drawer when clicking a table row', () => {
    cy.get('[data-testid="order-detail-drawer"]').should('not.be.visible')
    cy.contains('order-1').closest('tr').click()
    cy.get('[data-testid="order-detail-drawer"]').should('be.visible')
    cy.contains('AAPL.US').should('be.visible')
    cy.contains('149.5').should('be.visible')
  })

  it('shows note button in order detail drawer', () => {
    cy.contains('order-1').closest('tr').click()
    cy.get('[data-testid="order-detail-drawer"]').should('be.visible')
    cy.get('[data-testid="order-detail-note-btn"]').should('be.visible')
  })
})
