describe('Primary candidacy', () => {
  beforeEach(() => {
    cy.visitApp('/#/primary-candidacy')
    cy.wait('@getPrimaryCandidacy')
    cy.contains('主标的候选', { timeout: 10000 }).should('be.visible')
  })

  it('leads with the evidence verdict, not with a pick', () => {
    cy.get('[data-testid="candidacy-verdict"]').should('contain', '证据不支持切换')
  })

  it('withholds the edge pick and says why', () => {
    cy.get('[data-testid="pick-edge"]').should('contain', '证据不支持')
    cy.get('[data-testid="pick-edge"]').should('contain', 'POOL_SIGNAL_EDGE_BLOCKED')
  })

  it('shows the gates-only pick but refuses to call it an edge', () => {
    cy.get('[data-testid="pick-gates-only"]').should('contain', 'META.US')
    cy.get('[data-testid="pick-gates-only"]').should('contain', '非 edge 结论')
  })

  it('shows the tradeability pick as a cost choice only', () => {
    cy.get('[data-testid="pick-tradeability"]').should('contain', 'NVDA.US')
    cy.get('[data-testid="pick-tradeability"]').should('contain', '成本选择')
  })

  it('reports the pool gate block with its detail', () => {
    cy.get('[data-testid="pool-gate"]').should('contain', '已阻断')
    cy.get('[data-testid="pool-gate"]').should('contain', 'first-passage rate 29.7%')
  })

  it('states the required sample size against the best held sample', () => {
    cy.get('[data-testid="power-card"]').should('contain', '32')
    cy.get('[data-testid="power-card"]').should('contain', '8')
    cy.get('[data-testid="power-card"]').should('contain', 'TER.US')
  })

  it('marks the incumbent in the candidates table', () => {
    cy.get('[data-testid="candidates-table"]')
      .contains('tr', 'TSLA.US')
      .should('contain', '在任标的')
  })

  it('keeps the tradeability table caveated as cost-only', () => {
    cy.get('[data-testid="tradeability-table"]').should('contain', 'NVDA.US')
    cy.contains('仅成本口径').should('be.visible')
  })

  it('refetches with entry windows when opted in', () => {
    cy.get('[data-testid="include-entry-windows"] input').check({ force: true })
    cy.wait('@getPrimaryCandidacy')
      .its('request.url')
      .should('contain', 'include_entry_windows=true')
  })

})
