describe('Strategy LLM Card', () => {
  beforeEach(() => {
    cy.visitApp('/#/strategy')
  })

  it('displays LLM intelligent interval card', () => {
    cy.contains('LLM 智能区间').should('be.visible')
    cy.contains('启用').should('be.visible')
    cy.contains('禁用').should('be.visible')
  })

  it('has toggle switch for auto interval', () => {
    cy.get('.el-switch').first().should('exist')
  })

  it('has manual analyze button labeled for current strategy', () => {
    cy.contains('当前策略重新分析').should('be.visible')
  })

  it('displays recent LLM interaction history', () => {
    cy.wait('@getLLMInteractions')
    cy.contains('最近 LLM 交互').should('be.visible')
    cy.contains('NONE').should('be.visible')
    cy.contains('成功').should('be.visible')
  })

  it('displays LLM preview analysis card', () => {
    cy.contains('LLM 预览分析').should('be.visible')
    cy.contains('预览分析').should('be.visible')
  })

  it('preview button is disabled when symbol is empty', () => {
    cy.contains('button', '预览分析').should('be.disabled')
  })

  it('can enter symbol and trigger preview', () => {
    cy.get('input[placeholder="例如 AAPL.US"]').first().clear().type('AAPL.US')
    cy.contains('button', '预览分析').should('not.be.disabled').click()
    cy.wait('@previewLLMInterval')
    cy.contains('LLM 建议区间').should('be.visible')
    cy.contains('155.50').should('be.visible')
    cy.contains('198.80').should('be.visible')
  })

  it('shows apply button after successful preview', () => {
    cy.get('input[placeholder="例如 AAPL.US"]').first().clear().type('AAPL.US')
    cy.contains('button', '预览分析').click()
    cy.wait('@previewLLMInterval')
    cy.contains('应用到策略并保存').should('be.visible')
  })

  it('applies preview result to strategy form on confirm', () => {
    cy.get('input[placeholder="例如 AAPL.US"]').first().clear().type('AAPL.US')
    cy.contains('button', '预览分析').click()
    cy.wait('@previewLLMInterval')
    cy.contains('应用到策略并保存').click()
    cy.wait('@saveStrategy')
    cy.contains('已保存').should('be.visible')
  })

  it('shows error on preview failure', () => {
    cy.intercept('POST', '/api/strategy/llm-interval/preview', {
      body: {
        success: false,
        suggested_buy_low: 0,
        suggested_sell_high: 0,
        confidence_score: 0,
        analysis: '',
        applied: false,
        reason: '分析失败：无法获取行情数据',
      },
    }).as('previewFail')

    cy.get('input[placeholder="例如 AAPL.US"]').first().clear().type('INVALID.US')
    cy.contains('button', '预览分析').click()
    cy.wait('@previewFail')
    cy.contains('分析失败').should('be.visible')
    cy.contains('无法获取行情数据').should('be.visible')
  })

  it('does not report success when applying preview save fails', () => {
    cy.intercept('PUT', '/api/strategy', { statusCode: 500, body: { detail: 'save failed' } }).as('saveStrategyFail')

    cy.get('input[placeholder="例如 AAPL.US"]').first().clear().type('AAPL.US')
    cy.contains('button', '预览分析').click()
    cy.wait('@previewLLMInterval')
    cy.contains('应用到策略并保存').click()
    cy.wait('@saveStrategyFail')
    cy.contains('已将 LLM 建议应用到策略并保存').should('not.exist')
    cy.contains('保存失败').should('be.visible')
    cy.contains('应用到策略并保存').should('be.visible')
  })
})
