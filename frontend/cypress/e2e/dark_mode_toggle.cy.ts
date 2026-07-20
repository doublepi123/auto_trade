describe('Dark Mode Toggle', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.visit('/', {
      onBeforeLoad(win) {
        win.localStorage.removeItem('auto_trade.theme.dark')
      },
    })
  })

  it('toggles html.dark class and persists to localStorage', () => {
    cy.get('html').should('not.have.class', 'dark')
    cy.get('[data-testid="theme-toggle"]').should('have.attr', 'aria-label', '切换至深色模式').click()
    cy.get('html').should('have.class', 'dark')
    cy.window().then((win) => {
      expect(win.localStorage.getItem('auto_trade.theme.dark')).to.eq('1')
    })
    cy.get('[data-testid="theme-toggle"]').should('have.attr', 'aria-label', '切换至亮色模式').click()
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
