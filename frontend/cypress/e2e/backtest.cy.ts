describe('Backtest', () => {
  beforeEach(() => {
    cy.visitApp('/#/backtest')
    cy.contains('回测', { timeout: 10000 }).should('be.visible')
  })

  it('renders backtest inputs and actions', () => {
    cy.contains('参数').should('be.visible')
    cy.contains('历史数据').should('be.visible')
    cy.get('[data-testid="backtest-csv-input"]').should('be.visible')
    cy.get('[data-testid="run-backtest-button"]').should('be.visible')
  })

  it('runs backtest and displays metrics, charts, and trades', () => {
    cy.get('[data-testid="run-backtest-button"]').click()
    cy.wait('@runBacktest')

    cy.get('[data-testid="backtest-metrics"]').should('be.visible')
    cy.contains('总收益').should('be.visible')
    cy.contains('+$200.00').should('be.visible')
    cy.get('[data-testid="backtest-chart"]').should('be.visible')
    cy.get('[data-testid="backtest-trade-marker"]').should('exist')
    cy.get('[data-testid="backtest-trades"]').should('be.visible')
    cy.contains('买入').should('be.visible')
    cy.contains('卖出').should('be.visible')
    cy.contains('费用敏感性').should('be.visible')
  })

  it('can load sample csv after edits', () => {
    cy.get('[data-testid="backtest-csv-input"]').clear().type('bad')
    cy.contains('载入示例').click()
    cy.get('[data-testid="backtest-csv-input"]').should('contain.value', 'timestamp,open,high,low,close,volume')
  })
})
