describe('Strategy Full Config JSON Import/Export', () => {
  beforeEach(() => {
    cy.visitApp('/#/strategy')
    cy.wait('@getStrategy')
    cy.wait('@listStrategyPresets')
  })

  it('exports full strategy configuration as JSON', () => {
    cy.get('[data-testid="strategy-export-config"]').click()
  })

  it('imports full strategy configuration from JSON', () => {
    const config = {
      symbol: 'TSLA.US',
      market: 'US',
      buy_low: 150,
      sell_high: 250,
      short_selling: true,
      min_profit_amount: 5,
      fee_rate_us: 0.001,
      trading_session_mode: 'RTH_ONLY',
    }
    cy.writeFile('cypress/fixtures/temp-strategy-config.json', JSON.stringify(config))
    cy.get('[data-testid="strategy-import-config-input"]').selectFile('cypress/fixtures/temp-strategy-config.json', { force: true })
    cy.contains('配置已导入').should('be.visible')
    cy.contains('button', /^保存$/).should('not.be.disabled')
  })
})
