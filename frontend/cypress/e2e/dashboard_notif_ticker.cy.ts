describe('Dashboard Notification Ticker', () => {
  beforeEach(() => {
    cy.visitApp('/#/')
    cy.wait('@getStatus')
  })

  it('renders notification ticker with recent notifications', () => {
    cy.get('[data-testid="dashboard-notif-ticker"]').should('be.visible')
    cy.contains('风控熔断').should('be.visible')
    cy.contains('查看全部').should('be.visible')
  })

  it('navigates to notification center on item click', () => {
    cy.get('[data-testid="dashboard-notif-ticker"]')
      .find('.ticker-item')
      .first()
      .click()
    cy.url().should('include', '/notifications')
  })

  it('navigates to notification center on keyboard activation', () => {
    cy.get('[data-testid="dashboard-notif-ticker"]')
      .find('.ticker-item')
      .first()
      .focus()
      .type('{enter}')
    cy.url().should('include', '/notifications')
  })

  it('navigates to notification center on view all button click', () => {
    cy.get('[data-testid="dashboard-notif-ticker"]')
      .contains('查看全部')
      .click()
    cy.url().should('include', '/notifications')
  })

  it('keeps one ellipsized notification readable on mobile', () => {
    cy.viewport(390, 844)

    cy.get('[data-testid="dashboard-notif-ticker"] .ticker-item:visible')
      .should('have.length', 1)
      .find('.ticker-title')
      .should('have.css', 'text-overflow', 'ellipsis')
  })
})
