describe('Dashboard performance behavior', () => {
  beforeEach(() => {
    cy.stubApi()
  })

  it('renders status and controls while account request is delayed', () => {
    cy.intercept('GET', '**/api/account', (req) => {
      return new Cypress.Promise((resolve) => {
        setTimeout(() => {
          req.reply({ body: { total_assets: 10000, cash_balances: [], positions: [], available: true, error: null } })
          resolve()
        }, 5000)
      })
    }).as('slowAccount')

    cy.visit('/')
    cy.wait(500)
    cy.get('[data-testid="status-strip"]').should('be.visible')
    cy.get('[data-testid="quick-actions"]').should('be.visible')
    cy.contains('交易驾驶舱').should('be.visible')
    cy.get('[data-testid="position-panel"] .el-loading-mask').should('exist')
    cy.wait('@slowAccount')
  })

  it('does not create overlapping account refreshes', () => {
    cy.intercept('GET', '**/api/account', (req) => {
      return new Cypress.Promise((resolve) => {
        setTimeout(() => {
          req.reply({ body: { total_assets: 10000, cash_balances: [], positions: [], available: true, error: null } })
          resolve()
        }, 2000)
      })
    }).as('accountRefresh')

    cy.visit('/')
    cy.wait(1200)
    cy.get('@accountRefresh.all').should('have.length', 1)
  })
})
