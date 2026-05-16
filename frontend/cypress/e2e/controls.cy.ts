describe('Controls', () => {
  const strategy = {
    id: 1,
    symbol: 'AAPL.US',
    market: 'US',
    buy_low: 150,
    sell_high: 200,
    short_selling: false,
    max_daily_loss: 5000,
    max_consecutive_losses: 3,
    updated_at: '2026-01-01T00:00:00Z',
  }

  const account = {
    total_assets: 12345.67,
    cash_balances: [],
    positions: [],
  }

  function status(paused: boolean) {
    return {
      engine_state: 'flat',
      paused,
      kill_switch: false,
      daily_pnl: 0,
      consecutive_losses: 0,
      last_price: 180,
      last_trigger_price: 0,
      last_trigger_at: null,
    }
  }

  beforeEach(() => {
    let paused = false

    cy.intercept('GET', '/api/strategy', strategy).as('getStrategy')
    cy.intercept('GET', '/api/status', (req) => {
      req.reply(status(paused))
    }).as('getStatus')
    cy.intercept('GET', '/api/account', account).as('getAccount')
    cy.intercept('POST', '/api/control/pause', (req) => {
      expect(req.body).to.deep.equal({ reason: 'manual' })
      paused = true
      req.reply({ message: 'paused' })
    }).as('pauseTrading')
    cy.intercept('POST', '/api/control/resume', (req) => {
      expect(req.method).to.equal('POST')
      paused = false
      req.reply({ message: 'resumed' })
    }).as('resumeTrading')

    cy.visit('/')
    cy.wait('@getStrategy')
    cy.wait('@getStatus')
    cy.wait('@getAccount')
    cy.contains('.el-card', '操作控制', { timeout: 10000 }).should('be.visible')
  })

  it('can pause trading', () => {
    cy.contains('.el-card', '操作控制').within(() => {
      cy.get('button').contains('暂停').click({ force: true })
    })
    cy.wait('@pauseTrading')
    cy.wait('@getStatus')
    cy.contains('已暂停').should('be.visible')
  })

  it('can resume trading after pause', () => {
    cy.contains('.el-card', '操作控制').within(() => {
      cy.get('button').contains('暂停').click({ force: true })
    })
    cy.wait('@pauseTrading')
    cy.wait('@getStatus')
    cy.contains('已暂停').should('be.visible')
    cy.contains('.el-card', '操作控制').within(() => {
      cy.get('button').contains('恢复').click({ force: true })
    })
    cy.wait('@resumeTrading')
    cy.wait('@getStatus')
    cy.contains('运行中').should('be.visible')
  })

  it('shows emergency stop button', () => {
    cy.contains('.el-card', '操作控制').within(() => {
      cy.get('button').contains('紧急停止').should('be.visible')
    })
  })
})
