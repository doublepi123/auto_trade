describe('Dashboard', () => {
  beforeEach(() => {
    cy.visitApp('/')
    cy.contains('仪表盘', { timeout: 10000 }).should('be.visible')
  })

  it('displays engine state card', () => {
    cy.contains('引擎状态').should('be.visible')
  })

  it('displays price card', () => {
    cy.contains('最新价格').should('be.visible')
  })

  it('displays PnL card', () => {
    cy.contains('今日盈亏').should('be.visible')
  })

  it('displays total assets card', () => {
    cy.contains('总资产').should('be.visible')
  })

  it('displays cash balance card', () => {
    cy.contains('现金余额').should('be.visible')
  })

  it('displays positions card', () => {
    cy.contains('持仓明细').should('be.visible')
  })

  it('shows strategy info section', () => {
    cy.contains('行情信息').should('be.visible')
  })

  it('displays control buttons', () => {
    cy.contains('操作控制').should('be.visible')
    cy.get('button').contains('启动').should('be.visible')
    cy.get('button').contains('停止').should('be.visible')
  })

  it('displays risk status section', () => {
    cy.contains('风控状态').should('be.visible')
    cy.contains('紧急停止').should('be.visible')
    cy.contains('暂停状态').should('be.visible')
  })
})

describe('Dashboard – progressive loading', () => {
  it('renders status and controls while account request is delayed', () => {
    let accountRequests = 0
    cy.stubApi()
    cy.intercept('GET', '/api/account', (req) => {
      accountRequests += 1
      return new Promise<void>((resolve) => {
        setTimeout(() => {
          req.reply({ body: { total_assets: 12345, cash_balances: [], positions: [], available: true, error: null } })
          resolve()
        }, 2000)
      })
    }).as('slowAccount')

    cy.visit('/#/')
    cy.contains('引擎状态', { timeout: 1000 }).should('be.visible')
    cy.contains('操作控制').should('be.visible')
    cy.wrap(null).should(() => {
      expect(accountRequests).to.eq(1)
    })
    cy.wait('@slowAccount')
    cy.contains('$12345.00').should('be.visible')
  })

  it('does not overlap account refresh requests when the previous request is still running', () => {
    let accountRequests = 0
    let releaseAccount: (() => void) | null = null
    cy.stubApi()
    cy.intercept('GET', '/api/account', (req) => {
      accountRequests += 1
      return new Promise<void>((resolve) => {
        releaseAccount = () => {
          req.reply({ body: { total_assets: 100, cash_balances: [], positions: [], available: true, error: null } })
          resolve()
        }
      })
    }).as('slowAccount')

    cy.clock()
    cy.visit('/#/')
    cy.wrap(null).should(() => {
      expect(accountRequests).to.eq(1)
    })
    cy.tick(30000)
    cy.wrap(null).then(() => {
      expect(accountRequests).to.eq(1)
      expect(releaseAccount).to.be.a('function')
      releaseAccount?.()
    })
    cy.wait('@slowAccount')
  })

  it('shows card-level loading states while account and LLM requests are delayed', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/account', (req) => {
      return new Promise<void>((resolve) => {
        setTimeout(() => {
          req.reply({ body: { total_assets: 12345, cash_balances: [], positions: [], available: true, error: null } })
          resolve()
        }, 2000)
      })
    }).as('slowAccount')
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

    cy.visit('/#/')
    cy.contains('引擎状态', { timeout: 1000 }).should('be.visible')
    cy.contains('总资产').parents('.el-card').first().find('.el-skeleton').should('be.visible')
    cy.contains('现金余额').parents('.el-card').first().contains('账户数据加载中...').should('be.visible')
    cy.contains('持仓明细').parents('.el-card').first().contains('账户数据加载中...').should('be.visible')
    cy.contains('LLM 智能区间').should('be.visible')
    cy.contains('LLM 状态加载中...').should('be.visible')

    cy.wait('@slowAccount')
    cy.wait('@slowLLMStatus')
    cy.contains('$12345.00').should('be.visible')
    cy.contains('LLM 状态加载中...').should('not.exist')
  })

})
