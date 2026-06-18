describe('Notification Center Unread', () => {
  beforeEach(() => {
    localStorage.removeItem('notifications_last_read_at')
    cy.visitApp('/#/notifications')
    cy.wait('@getNotifications')
  })

  it('shows unread count badge and dots when there are unread notifications', () => {
    cy.get('[data-testid="notif-unread-badge"]').should('contain', '3')
    cy.get('[data-testid="notif-unread-count"]').should('contain', '未读 3')
    cy.get('.unread-dot').should('have.length.at.least', 1)
  })

  it('marks all as read and clears badge/dots', () => {
    cy.get('[data-testid="notif-mark-all-read"]').click()
    cy.get('[data-testid="notif-unread-badge"] sup').should('not.exist')
    cy.get('[data-testid="notif-unread-count"]').should('not.exist')
    cy.get('.unread-dot').should('not.exist')
    cy.window().its('localStorage').invoke('getItem', 'notifications_last_read_at').should('not.be.null')
  })

  it('shows nav notification badge on dashboard', () => {
    cy.visitApp('/#/')
    cy.wait('@getStatus')
    cy.get('[data-testid="nav-notif-badge"]').should('contain', '3')
  })
})
