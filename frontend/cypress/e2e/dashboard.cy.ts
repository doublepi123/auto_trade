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

  it('renders the unrealized-PnL panel', () => {
    cy.get('[data-testid="position-pnl-panel"]').should('be.visible')
    cy.contains('持仓浮盈').should('be.visible')
  })

  it('renders the risk-history sparkline', () => {
    cy.get('[data-testid="risk-history-panel"]').should('be.visible')
    cy.get('[data-testid="risk-sparkline"]').should('exist')
  })

  it('renders the market session clock', () => {
    cy.get('[data-testid="session-panel"]').should('be.visible')
    cy.get('[data-testid="session-status"]').should('contain', '交易中')
  })

  it('shows strategy info section', () => {
    cy.contains('行情信息').should('be.visible')
  })

  it('shows recent decision timeline events', () => {
    cy.contains('决策时间线').should('be.visible')
    cy.contains('LLM 分析').should('be.visible')
    cy.contains('区间测试').should('be.visible')
    cy.contains('expected profit 4.00 is below required minimum profit 5.00').should('be.visible')
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
    cy.get('[data-testid="drawdown-risk-status"]')
      .should('contain', '高水位回撤：$100.00 / $250.00')
      .and('contain', '峰值 $1000.00')
  })

  it('shows the latest trigger skip reason', () => {
    cy.intercept('GET', '/api/status', {
      body: {
        engine_state: 'long',
        paused: false,
        kill_switch: false,
        runner_running: true,
        daily_pnl: 0,
        consecutive_losses: 0,
        last_price: 220.84,
        last_trigger_price: 220.84,
        last_trigger_at: '2026-05-22T12:42:03Z',
        last_action_message: 'SELL skipped: no long position for NVDA.US',
      },
    }).as('statusWithActionMessage')

    cy.visit('/')
    cy.wait('@statusWithActionMessage')

    cy.get('[data-testid="price-panel"]').should('contain', '最近动作')
    cy.get('[data-testid="price-panel"]').should('contain', 'SELL skipped')
  })

  it('shows an at-a-glance cockpit summary above the fold', () => {
    cy.intercept('GET', '/api/strategy', {
      body: {
        id: 1,
        symbol: 'NVDA.US',
        market: 'US',
        buy_low: 219.7,
        sell_high: 220.3,
        short_selling: false,
        max_daily_loss: 5000,
        max_consecutive_losses: 3,
        min_profit_amount: 0,
        auto_resume_minutes: 3,
        llm_interval_minutes: 1,
        updated_at: '2026-05-22T11:15:31Z',
      },
    }).as('cockpitStrategy')
    cy.intercept('GET', '/api/status', {
      body: {
        engine_state: 'long',
        paused: true,
        kill_switch: false,
        runner_running: true,
        daily_pnl: 0,
        consecutive_losses: 0,
        last_price: 219.99,
        last_trigger_price: 219.9,
        last_trigger_at: null,
        last_action_message: '',
      },
    }).as('cockpitStatus')
    cy.intercept('GET', '/api/account', {
      body: {
        total_assets: 32092.27,
        cash_balances: [
          { currency: 'USD', available_cash: -4556.93, frozen_cash: 18.78 },
          { currency: 'HKD', available_cash: 36621.45, frozen_cash: 0 },
        ],
        positions: [
          { symbol: 'NVDA.US', side: 'LONG', quantity: 18, avg_price: 226.612, market_value: 3951.18 },
        ],
        available: true,
        error: null,
      },
    }).as('cockpitAccount')
    cy.intercept('GET', '/api/orders*', {
      body: {
        items: [
          {
            id: 1,
            broker_order_id: 'order-1',
            symbol: 'NVDA.US',
            side: 'SELL',
            quantity: 1,
            price: 218.51,
            executed_quantity: 1,
            executed_price: 219.8,
            status: 'FILLED',
            created_at: '2026-05-22T10:59:02Z',
            filled_at: '2026-05-22T10:59:03Z',
            source: 'local',
            cancellable: false,
          },
        ],
        total: 1,
        page: 1,
        page_size: 5,
        scope: 'today',
      },
    }).as('cockpitOrders')

    cy.visit('/')
    cy.wait(['@cockpitStrategy', '@cockpitStatus', '@cockpitAccount', '@cockpitOrders'])

    cy.get('[data-testid="dashboard-cockpit"]').should('be.visible')
    cy.get('[data-testid="status-strip"]').should('contain', 'NVDA.US').and('contain', '已暂停')
    cy.get('[data-testid="price-panel"]').should('contain', '$219.99').and('contain', '买入线').and('contain', '卖出线')
    cy.get('[data-testid="position-panel"]').should('contain', '18').and('contain', '$226.61').and('contain', '浮动盈亏')
    cy.get('[data-testid="llm-panel"]').should('contain', '区间测试').and('contain', '0.75')
    cy.get('[data-testid="quick-actions"]').should('contain', '恢复').and('contain', '紧急停止')
    cy.get('[data-testid="recent-orders"]').should('contain', 'SELL').and('contain', 'FILLED')
  })

  it('shows the skipped order category in recent events', () => {
    cy.visitApp('/')
    cy.get('[data-testid="recent-events"]').should('contain', '成本不足')
  })

  it('shows read-only multi-symbol snapshots', () => {
    cy.visitApp('/')

    cy.contains('多标的观察').should('be.visible')
    cy.contains('NVDA.US').should('be.visible')
    cy.contains('Nvidia').should('be.visible')
    cy.contains('当前交易').should('be.visible')
    cy.contains('AAPL.US').should('be.visible')
  })

  it('shows empty multi-symbol snapshot state', () => {
    cy.intercept('GET', '/api/watchlist/snapshots', { body: [] })
    cy.visit('/')

    cy.contains('多标的观察').should('be.visible')
    cy.contains('暂无观察标的').should('be.visible')
  })

  it('shows diagnostics panel with runner and symbol runtime health', () => {
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: ['AAPL.US'],
        live_safety: {
          short_entries_enabled: false,
          allow_position_addons: false,
          max_position_quantity: 80,
          max_position_notional: 4000,
          max_risk_per_trade: 200,
          stop_loss_pct: 0.8,
          max_holding_minutes: 45,
          entry_cutoff_minutes_before_close: 50,
          flatten_minutes_before_close: 20,
          llm_shadow_mode: true,
          llm_order_execution_enabled: false,
        },
        quote_stream: {
          last_push_age_seconds: 3,
          last_quote_age_seconds: 1,
          recent_quote_count: 12,
        },
        risk: {
          paused: false,
          kill_switch: false,
          pause_reason: '',
          daily_pnl: 12.5,
          consecutive_losses: 1,
        },
        symbol_runtimes: [
          {
            symbol: 'NVDA.US',
            market: 'US',
            is_primary: true,
            engine_state: 'long',
            last_price: 221.2,
            last_trigger_price: 220.6,
            recent_quote_count: 5,
            has_pending_order: false,
            position_quantity: 1088,
            position_avg_price: 206.329,
            position_notional: 240665.6,
            position_risk_at_stop: 2244.86,
            position_limit_breaches: ['MAX_POSITION_QUANTITY', 'MAX_POSITION_NOTIONAL', 'MAX_RISK_PER_TRADE'],
          },
          {
            symbol: 'AAPL.US',
            market: 'US',
            is_primary: false,
            engine_state: 'flat',
            last_price: 199.5,
            last_trigger_price: 0,
            recent_quote_count: 7,
            has_pending_order: true,
            position_quantity: 0,
            position_avg_price: 0,
            position_notional: 0,
            position_risk_at_stop: 0,
            position_limit_breaches: [],
          },
        ],
      },
    }).as('getDiagnostics')

    cy.visit('/')
    cy.wait('@getDiagnostics')

    cy.contains('运行诊断').should('be.visible')
    cy.contains('行情流').should('be.visible')
    cy.contains('AAPL.US').should('be.visible')
    cy.contains('超限 3 项').should('be.visible')
    cy.contains('线程存活').should('be.visible')
    cy.contains('最近推送 3.0s').should('be.visible')
    cy.get('[data-testid="dashboard-live-safety"]')
      .should('contain', '实时安全参数')
      .and('contain', '做空开仓')
      .and('contain', '关闭')
      .and('contain', '4000.00')
      .and('contain', '45 分钟')
      .and('contain', '影子')
      .and('contain', '禁下单')
  })

  it('clarifies that control actions apply globally across symbol runtimes', () => {
    cy.visitApp('/')
    cy.get('[data-testid="quick-actions"]').should('contain', '全局控制')
    cy.get('[data-testid="quick-actions"]').should('contain', '作用于全部标的运行时')
    cy.get('[data-testid="dashboard-diagnostics"]').should('contain', '运行时总数')
    cy.get('[data-testid="dashboard-diagnostics"]').should('contain', '2 个')
    cy.contains('全局紧急停止').should('be.visible')
    cy.contains('全局暂停状态').should('be.visible')
  })
})
