describe('Dark Mode Toggle', () => {
  beforeEach(() => {
    cy.visitApp('/')
  })

  it('toggles html.dark class and persists to localStorage', () => {
    cy.get('html').should('not.have.class', 'dark')
    cy.get('[data-testid="nav-theme-toggle"]').should('contain', '深色').click()
    cy.get('html').should('have.class', 'dark')
    cy.window().then((win) => {
      expect(win.localStorage.getItem('auto_trade.theme.dark')).to.eq('1')
    })
    cy.get('[data-testid="nav-theme-toggle"]').should('contain', '亮色').click()
    cy.get('html').should('not.have.class', 'dark')
  })

  it('restores dark mode from localStorage on reload', () => {
    cy.window().then((win) => {
      win.localStorage.setItem('auto_trade.theme.dark', '1')
    })
    cy.reload()
    cy.get('html').should('have.class', 'dark')
  })
})
