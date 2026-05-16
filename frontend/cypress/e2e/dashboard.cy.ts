describe('Dashboard', () => {
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

  const status = {
    engine_state: 'flat',
    paused: false,
    kill_switch: false,
    daily_pnl: 0,
    consecutive_losses: 0,
    last_price: 180,
    last_trigger_price: 0,
    last_trigger_at: null,
  }

  const account = {
    total_assets: 12345.67,
    cash_balances: [{ currency: 'USD', available_cash: 12000, frozen_cash: 0 }],
    positions: [{ symbol: 'AAPL.US', side: 'long', quantity: 10, avg_price: 180, market_value: 1800 }],
  }

  function visitDashboard() {
    cy.intercept('GET', '/api/strategy', strategy).as('getStrategy')
    cy.intercept('GET', '/api/status', status).as('getStatus')
    cy.visit('/')
    cy.wait('@getStrategy')
    cy.wait('@getStatus')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
  }

  it('displays dashboard sections', () => {
    cy.intercept('GET', '/api/account', account).as('getAccount')
    visitDashboard()
    cy.wait('@getAccount')

    cy.get('.el-card__header').should('contain', '连接状态')
    cy.get('.el-card__header').should('contain', '策略状态')
    cy.get('.el-card__header').should('contain', '账户摘要')
    cy.get('.el-card__header').should('contain', '风控状态')
    cy.get('.el-card__header').should('contain', '操作控制')
    cy.get('.el-card__header').should('contain', '持仓明细')
  })

  it('displays account data when account request succeeds', () => {
    cy.intercept('GET', '/api/account', account).as('getAccount')
    visitDashboard()
    cy.wait('@getAccount')

    cy.contains('.el-card', '账户摘要').within(() => {
      cy.get('h1').should('contain', '$12345.67')
      cy.contains('账户数据暂不可用').should('not.exist')
    })
  })

  it('displays account unavailable fallback when account request fails', () => {
    cy.intercept('GET', '/api/account', { statusCode: 500, body: { detail: 'broker unavailable' } }).as('getAccount')
    visitDashboard()
    cy.wait('@getAccount')

    cy.contains('.el-card', '账户摘要').within(() => {
      cy.contains('账户数据暂不可用').should('be.visible')
      cy.get('h1').should('not.exist')
    })
  })

  it('displays control buttons', () => {
    cy.intercept('GET', '/api/account', account).as('getAccount')
    visitDashboard()
    cy.wait('@getAccount')

    cy.contains('.el-card', '操作控制').within(() => {
      cy.get('button').contains('启动').should('be.visible')
      cy.get('button').contains('停止').should('be.visible')
    })
  })

  it('displays risk status section', () => {
    cy.intercept('GET', '/api/account', account).as('getAccount')
    visitDashboard()
    cy.wait('@getAccount')

    cy.contains('风控状态').should('be.visible')
    cy.contains('紧急停止').should('be.visible')
    cy.contains('暂停状态').should('be.visible')
  })
})
