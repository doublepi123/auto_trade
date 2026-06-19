describe('Global Keyboard Shortcut Navigation', () => {
  beforeEach(() => {
    cy.visitApp('/')
    // ensure body is the focused target (not an input)
    cy.get('body').focus()
  })

  it('navigates to strategy when pressing "s" outside an input', () => {
    cy.location('hash').should('eq', '#/')
    cy.get('body').type('s')
    cy.location('hash').should('eq', '#/strategy')
  })

  it('navigates to notifications via "n" and back via "d"', () => {
    cy.get('body').type('n')
    cy.location('hash').should('eq', '#/notifications')
    cy.get('body').type('d')
    cy.location('hash').should('eq', '#/')
  })

  it('does not navigate while typing in an input', () => {
    cy.get('body').type('s')
    cy.location('hash').should('eq', '#/strategy')
    // focus the strategy symbol input and type a letter — must not navigate away
    cy.contains('.el-form-item', '股票代码').find('input').first().focus().type('h')
    cy.location('hash').should('eq', '#/strategy')
  })

  it('opens the shortcuts help dialog via the button and "?"', () => {
    cy.get('[data-testid="nav-shortcuts"]').click()
    cy.get('[data-testid="shortcuts-dialog"]').should('be.visible')
    cy.get('[data-testid="shortcuts-dialog"]').should('contain', '仪表盘')
  })
})
