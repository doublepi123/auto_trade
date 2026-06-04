describe('Notification Center (P23 Toast)', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.visitApp()
    cy.visit('/#/dashboard')
  })

  it('opens notification settings dialog', () => {
    cy.get('[data-testid="nav-notification-settings"]').should('be.visible').click()
    cy.get('[data-testid="notification-settings"]').should('be.visible')
    cy.get('[data-testid="notification-save-btn"]').should('be.visible')
  })

  it('dashboard control buttons have testids', () => {
    cy.get('[data-testid="dashboard-start-btn"]').should('be.visible')
    cy.get('[data-testid="dashboard-resume-btn"]').should('be.visible')
    cy.get('[data-testid="dashboard-pause-btn"]').should('be.visible')
    cy.get('[data-testid="dashboard-stop-btn"]').should('be.visible')
    cy.get('[data-testid="dashboard-kill-btn"]').should('be.visible')
  })
})
