describe('Strategy', () => {
  beforeEach(() => {
    cy.intercept('GET', '/api/strategy', {
      id: 1,
      symbol: 'AAPL.US',
      market: 'US',
      buy_low: 180,
      sell_high: 200,
      short_selling: false,
      max_daily_loss: 5000,
      max_consecutive_losses: 3,
      updated_at: '2026-01-01T00:00:00Z',
    }).as('getStrategy')
    cy.visit('/#/strategy')
    cy.wait('@getStrategy')
    cy.contains('策略配置', { timeout: 10000 }).should('be.visible')
  })

  it('displays strategy form fields', () => {
    cy.contains('股票代码').should('be.visible')
    cy.contains('买入价下限').should('be.visible')
    cy.contains('市场').should('be.visible')
  })

  it('has save button', () => {
    cy.contains('button', '保存').should('be.visible')
  })

  it('shows an alert when saving fails', () => {
    cy.intercept('PUT', '/api/strategy', { statusCode: 500, body: {} }).as('updateStrategy')

    cy.get('input').first().should('have.value', 'AAPL.US')
    cy.get('.el-form').trigger('submit')

    cy.wait('@updateStrategy')
    cy.get('.el-alert').should('contain', '保存失败')
  })

  it('submits strategy only once when save is clicked rapidly', () => {
    cy.intercept('PUT', '/api/strategy', (req) => {
      req.reply({
        delay: 200,
        body: {
          id: 1,
          symbol: 'AAPL.US',
          market: 'US',
          buy_low: 180,
          sell_high: 200,
          short_selling: false,
          max_daily_loss: 5000,
          max_consecutive_losses: 3,
          updated_at: '2026-01-01T00:00:00Z',
        },
      })
    }).as('updateStrategy')

    cy.get('input').first().should('have.value', 'AAPL.US')
    cy.get('.el-form').trigger('submit').trigger('submit')

    cy.wait('@updateStrategy')
    cy.get('@updateStrategy.all').should('have.length', 1)
  })
})
