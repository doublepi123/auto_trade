describe('Credentials Notification Channel Test', () => {
  beforeEach(() => {
    cy.visitApp('/#/credentials')
    cy.wait('@getCredentials')
  })

  it('renders per-channel test button', () => {
    cy.get('[data-testid="notification-channel-row"]').first().find('[data-testid="channel-test-btn"]').should('be.visible')
  })

  it('sends a single channel test and shows success', () => {
    cy.get('[data-testid="notification-channel-row"]').first().find('[data-testid="channel-test-btn"]').click()
    cy.wait('@testNotificationChannel')
    cy.get('[data-testid="notification-channel-row"]').first().find('[data-testid="channel-test-success"]').should('be.visible')
  })
})
