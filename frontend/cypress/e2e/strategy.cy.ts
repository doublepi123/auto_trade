describe('Strategy', () => {
  beforeEach(() => {
    cy.visitApp('/#/strategy')
    cy.contains('策略配置', { timeout: 10000 }).should('be.visible')
  })

  it('displays strategy form fields', () => {
    cy.contains('股票代码').should('be.visible')
    cy.contains('买入价下限').should('be.visible')
    cy.contains('市场').should('be.visible')
    cy.contains('单笔最低盈利金额').should('be.visible')
    cy.contains('暂停自动恢复（分钟）').should('be.visible')
  })

  it('has save button', () => {
    cy.get('.el-button--primary').should('be.visible')
  })

  it('keeps default live-safety limits valid for native form submission', () => {
    for (const testId of ['max-position-notional', 'max-risk-per-trade', 'stop-loss-pct', 'max-holding-minutes']) {
      cy.get(`[data-testid="${testId}"] input`).then(($input) => {
        expect(($input[0] as HTMLInputElement).checkValidity(), testId).to.equal(true)
      })
    }
  })

  it('accepts cent-level decimal prices without native number validation errors', () => {
    cy.contains('.el-form-item', '买入价下限')
      .find('input')
      .should('have.attr', 'step', '0.01')
      .clear()
      .type('218.50')
      .then(($input) => {
        expect(($input[0] as HTMLInputElement).checkValidity()).to.equal(true)
      })

    cy.contains('.el-form-item', '卖出价上限')
      .find('input')
      .should('have.attr', 'step', '0.01')
      .clear()
      .type('219.50')
      .then(($input) => {
        expect(($input[0] as HTMLInputElement).checkValidity()).to.equal(true)
      })

    cy.contains('.el-form-item', '单笔最低盈利金额')
      .find('input')
      .should('have.attr', 'step', '0.01')
      .clear()
      .type('5.50')
      .then(($input) => {
        expect(($input[0] as HTMLInputElement).checkValidity()).to.equal(true)
      })

    cy.contains('.el-form-item', '暂停自动恢复（分钟）')
      .find('input')
      .should('have.attr', 'step', '1')
      .clear()
      .type('3')
      .then(($input) => {
        expect(($input[0] as HTMLInputElement).checkValidity()).to.equal(true)
      })
  })

  it('edits cost and LLM execution protection settings', () => {
    cy.intercept('PUT', '/api/strategy', (req) => {
      expect(req.body.fee_rate_us).to.equal(0.001)
      expect(req.body.fee_rate_hk).to.equal(0.004)
      expect(req.body.min_repricing_pct).to.equal(0.005)
      expect(req.body.llm_action_cooldown_seconds).to.equal(120)
      req.reply({ statusCode: 200, body: Object.assign({ id: 1, updated_at: '2026-05-25T00:00:00Z' }, req.body) })
    }).as('saveSafetySettings')

    cy.contains('美股单边预估费率').parent().find('input').clear().type('0.10')
    cy.contains('港股单边预估费率').parent().find('input').clear().type('0.40')
    cy.contains('LLM 最小改价').parent().find('input').clear().type('0.50')
    cy.contains('LLM 同向冷却').parent().find('input').clear().type('120')
    cy.contains('button', /^保存$/).click()
    cy.wait('@saveSafetySettings')
  })

  it('renders the scheduled-report section and sends on demand', () => {
    cy.contains('定时报告').should('be.visible')
    cy.get('[data-testid="report-schedule-test"]').click()
    cy.wait('@runScheduledReport')
    cy.contains('已发送').should('be.visible')
  })

  it('saves and lists a strategy preset', () => {
    cy.get('input[placeholder="如：保守 / 激进"]').type('保守')
    cy.get('[data-testid="preset-save"]').click()
    cy.wait('@createStrategyPreset')
    cy.contains('预设已保存').should('be.visible')
  })
})
