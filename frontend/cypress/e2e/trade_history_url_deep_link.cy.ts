describe('TradeHistory Filter URL Deep-linking', () => {
  it('hydrates round-trip filters from the URL query on load', () => {
    cy.stubApi()
    cy.visit('/#/history?filter=winners&symbol=NVIDIA')
    cy.wait('@getOrders')
    cy.contains('已实现成交（往返配对').click()
    // filter=winners button should be active; symbol search box carries NVIDIA
    cy.get('[data-testid="roundtrip-filter-winners"]').should('have.class', 'el-button--primary')
    cy.get('[data-testid="roundtrip-symbol-search"]').should('have.value', 'NVIDIA')
  })

  it('pushes filter changes into the URL query', () => {
    cy.stubApi()
    cy.visit('/#/history')
    cy.wait('@getOrders')
    cy.contains('已实现成交（往返配对').click()
    cy.get('[data-testid="roundtrip-filter-losers"]').click()
    // debounced 300ms sync; hash router stores the query inside the hash
    cy.location('hash').should('include', 'filter=losers')
  })
})
