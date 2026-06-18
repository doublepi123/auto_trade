describe('Strategy Preset JSON Import/Export', () => {
  beforeEach(() => {
    cy.visitApp('/#/strategy')
    cy.wait('@getStrategy')
    cy.wait('@listStrategyPresets')
  })

  it('exports current strategy parameters as JSON', () => {
    cy.get('[data-testid="preset-export-current"]').click()
  })

  it('imports a JSON preset file and creates a preset', () => {
    const preset = { name: 'imported-preset', params: { symbol: 'TSLA.US', market: 'US', buy_low: 100, sell_high: 200 } }
    cy.writeFile('cypress/fixtures/temp-preset.json', JSON.stringify(preset))
    cy.get('[data-testid="preset-import-input"]').selectFile('cypress/fixtures/temp-preset.json', { force: true })
    cy.wait('@createStrategyPreset')
    cy.get('[data-testid="preset-select"] .el-select__wrapper').click()
    cy.get('.el-select-dropdown__item').contains('imported-preset').should('be.visible')
  })

  it('imports an array of JSON presets', () => {
    const presets = [
      { name: 'array-preset-1', params: { symbol: 'AAPL.US', market: 'US', buy_low: 150, sell_high: 160 } },
      { name: 'array-preset-2', params: { symbol: 'BABA.US', market: 'US', buy_low: 80, sell_high: 90 } },
    ]
    cy.writeFile('cypress/fixtures/temp-presets.json', JSON.stringify(presets))
    cy.get('[data-testid="preset-import-input"]').selectFile('cypress/fixtures/temp-presets.json', { force: true })
    cy.wait('@createStrategyPreset')
    cy.get('[data-testid="preset-select"] .el-select__wrapper').click()
    cy.get('.el-select-dropdown__item').contains('array-preset-1').should('be.visible')
  })
})
