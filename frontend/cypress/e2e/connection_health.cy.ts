describe('Global connection health', () => {
  beforeEach(() => {
    cy.visitApp('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
  })

  it('shows the health badge in the desktop header', () => {
    cy.get('[data-testid="desktop-nav"]').should('be.visible')
    cy.get('[data-testid="nav-health"]').should('be.visible').and('contain', '轮询')
  })

  it('opens a health popover with connection + runner state', () => {
    cy.get('[data-testid="nav-health"]').click()
    cy.get('[data-testid="health-panel"]').should('be.visible')
    cy.get('[data-testid="health-panel"]').should('contain', '实时连接')
    cy.get('[data-testid="health-panel"]').should('contain', '运行器')
    cy.get('[data-testid="health-reconnect"]').should('be.visible')
  })

  it('keeps polling mode after clicking reconnect under Cypress', () => {
    cy.get('[data-testid="nav-health"]').click()
    cy.get('[data-testid="health-reconnect"]').click()
    cy.get('[data-testid="nav-health"]').should('contain', '轮询')
  })

  it('keeps the badge visible after navigating away from the dashboard', () => {
    cy.get('[data-testid="nav-health"]').should('contain', '轮询')
    cy.contains('a', '观察列表').click()
    cy.contains('观察列表', { timeout: 10000 }).should('be.visible')
    cy.get('[data-testid="nav-health"]').should('be.visible').and('contain', '轮询')
  })

  it('offers a copy-health-snapshot action in the popover', () => {
    cy.get('[data-testid="nav-health"]').click()
    cy.get('[data-testid="health-copy-snapshot"]').should('be.visible').and('contain', '复制健康快照')
  })

  it('surfaces data age in the badge and popover once a poll lands', () => {
    // The 3s poll marks the stream fresh on success; the age suffix + popover
    // age row then appear.
    cy.get('[data-testid="nav-health-age"]', { timeout: 12000 }).should('exist')
    cy.get('[data-testid="nav-health"]').click()
    cy.get('[data-testid="health-age"]').should('not.contain', '—')
  })
})
