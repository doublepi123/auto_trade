describe('Strategy', () => {
  beforeEach(function () {
    if (this.currentTest?.title.includes('during initial load')) return

    cy.visitApp('/#/strategy')
    cy.contains('策略配置', { timeout: 10000 }).should('be.visible')
  })

  it('displays strategy form fields', () => {
    cy.contains('股票代码').should('be.visible')
    cy.contains('买入价下限').should('be.visible')
    cy.contains('市场').should('be.visible')
  })

  it('has save button', () => {
    cy.get('.el-button--primary').should('be.visible')
  })

  it('disables strategy form and shows scoped loading states during initial load', () => {
    cy.intercept('GET', '/api/strategy', (req) => {
      return new Promise<void>((resolve) => {
        setTimeout(() => {
          req.reply({
            body: {
              id: 1, symbol: 'AAPL.US', market: 'US', buy_low: 100, sell_high: 200,
              short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
              llm_interval_minutes: 240,
              updated_at: '2026-01-01T00:00:00Z',
            },
          })
          resolve()
        }, 2000)
      })
    }).as('slowStrategy')
    cy.intercept('GET', '/api/strategy/llm-interval/status', (req) => {
      return new Promise<void>((resolve) => {
        setTimeout(() => {
          req.reply({
            body: {
              enabled: true,
              interval_minutes: 1,
              last_analysis_at: null,
              next_analysis_at: null,
              current_suggestion: null,
              applied_values: null,
              reject_reason: null,
            },
          })
          resolve()
        }, 2000)
      })
    }).as('slowLLMStatus')

    cy.visit('/#/strategy')
    cy.contains('策略配置加载中...').should('be.visible')
    cy.contains('LLM 状态加载中...').should('be.visible')
    cy.contains('button', '保存').parents('.el-card').first().as('strategyConfigCard')
    cy.get('@strategyConfigCard').contains('label', '股票代码').parents('.el-form-item').first().find('input').should('be.disabled')
    cy.get('@strategyConfigCard').contains('button', '保存').should('be.disabled')

    cy.wait('@slowStrategy')
    cy.get('@strategyConfigCard').contains('策略配置加载中...').should('not.exist')
    cy.get('@strategyConfigCard').contains('label', '股票代码').parents('.el-form-item').first().find('input').should('not.be.disabled')
    cy.wait('@slowLLMStatus')
    cy.contains('LLM 状态加载中...').should('not.exist')
  })
})
