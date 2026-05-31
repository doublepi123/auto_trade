type EngineState = 'flat' | 'long' | 'short'

interface StatusStub {
  engine_state: EngineState
  paused: boolean
  kill_switch: boolean
  runner_running: boolean
  daily_pnl: number
  consecutive_losses: number
  last_price: number
  last_trigger_price: number
  last_trigger_at: string | null
  last_action_message: string
  trading_session_mode: 'ANY' | 'RTH_ONLY'
  is_trading_hours: boolean
}

function initialStatus(): StatusStub {
  return {
    engine_state: 'flat',
    paused: false,
    kill_switch: false,
    runner_running: false,
    daily_pnl: 0,
    consecutive_losses: 0,
    last_price: 0,
    last_trigger_price: 0,
    last_trigger_at: null,
    last_action_message: '',
    trading_session_mode: 'ANY',
    is_trading_hours: true,
  }
}

Cypress.Commands.add('setupApp', () => {
  // No-op: API key auth removed
})

Cypress.Commands.add('stubApi', () => {
  let status = initialStatus()

  cy.intercept('GET', '/api/strategy', {
    body: {
      id: 1, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
      short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
      min_profit_amount: 0,
      auto_resume_minutes: 3,
      llm_interval_minutes: 2,
      fee_rate_us: 0.0005,
      fee_rate_hk: 0.003,
      min_repricing_pct: 0.003,
      llm_action_cooldown_seconds: 60,
      trading_session_mode: 'ANY',
      updated_at: '2026-01-01T00:00:00Z',
    },
  }).as('getStrategy')

  cy.intercept('GET', '/api/status', (req) => {
    req.reply({ body: status })
  }).as('getStatus')

  cy.intercept('GET', '/api/status/history*', {
    body: {
      points: [
        {
          timestamp: '2026-05-22T10:00:00Z',
          engine_state: 'flat',
          paused: false,
          kill_switch: false,
          daily_pnl: 0,
          consecutive_losses: 0,
          last_price: 220.1,
          last_trigger_price: 0,
        },
        {
          timestamp: '2026-05-22T10:01:00Z',
          engine_state: 'long',
          paused: false,
          kill_switch: false,
          daily_pnl: 12.5,
          consecutive_losses: 0,
          last_price: 221.2,
          last_trigger_price: 220.6,
        },
      ],
      markers: [
        {
          timestamp: '2026-05-22T10:01:00Z',
          broker_order_id: 'filled-1',
          symbol: 'NVDA.US',
          side: 'BUY',
          quantity: 3,
          price: 220.6,
          status: 'FILLED',
        },
      ],
    },
  }).as('getStatusHistory')

  cy.intercept('GET', '/api/account', {
    body: { total_assets: 0, cash_balances: [], positions: [], available: true, error: null },
  }).as('getAccount')

  cy.intercept('GET', '/api/credentials', {
    body: {
      id: 1, longbridge_app_key: '', longbridge_app_secret: '',
      longbridge_access_token: '', sct_key: '',
      has_longbridge_app_key: false, has_longbridge_app_secret: false,
      has_longbridge_access_token: false, has_sct_key: false,
      notification_channels: [{ type: 'serverchan', severity_floor: 'INFO' }],
      updated_at: '2026-01-01T00:00:00Z',
    },
  }).as('getCredentials')

  cy.intercept('GET', '/api/orders*', {
    body: {
      items: [],
      total: 0,
      page: 1,
      page_size: 10,
      scope: 'today',
    },
  }).as('getOrders')

  cy.intercept('GET', '/api/events*', {
    body: {
      items: [
        {
          id: 1,
          source: 'trade',
          event_type: 'LLM_ANALYSIS',
          symbol: 'NVDA.US',
          broker_order_id: '',
          side: '',
          status: 'SUCCESS',
          message: '区间测试',
          payload: { confidence_score: 0.75 },
          created_at: '2026-05-19T19:52:03.545862Z',
        },
        {
          id: 2,
          source: 'trade',
          event_type: 'ORDER_SKIPPED',
          symbol: 'NVDA.US',
          broker_order_id: '',
          side: 'SELL',
          status: 'SKIPPED',
          message: 'expected profit 4.00 is below required minimum profit 5.00',
          payload: { skip_category: 'FEE', expected_profit: 4, estimated_fees: 1, required_profit: 5 },
          created_at: '2026-05-19T19:53:03.545862Z',
        },
      ],
      total: 2,
      page: 1,
      page_size: 20,
    },
  }).as('getEvents')

  cy.intercept('GET', '/api/strategy/llm-interval/status', {
    body: {
      enabled: true,
      interval_minutes: 1,
      last_analysis_at: '2026-05-19T19:52:03.545862Z',
      next_analysis_at: '2026-05-19T19:53:03.545862Z',
      current_suggestion: {
        buy_low: 220.42,
        sell_high: 221.42,
        confidence_score: 0.75,
        analysis: '区间测试',
      },
      applied_values: { buy_low: 220.42, sell_high: 221.42 },
      reject_reason: null,
    },
  }).as('getLLMIntervalStatus')

  cy.intercept('GET', '/api/strategy/llm-interval/interactions*', {
    body: [
      {
        id: 1,
        interaction_type: 'analyze',
        symbol: 'AAPL.US',
        market: 'US',
        success: true,
        error: '',
        order_action: 'NONE',
        order_status: null,
        order_id: null,
        applied: true,
        created_at: '2026-05-19T19:52:03.545862Z',
      },
    ],
  }).as('getLLMInteractions')

  cy.intercept('POST', '/api/backtest/run', {
    body: {
      params: {
        symbol: 'AAPL.US',
        buy_low: 100,
        sell_high: 200,
        short_selling: false,
        min_profit_amount: 0,
        max_daily_loss: 5000,
        max_consecutive_losses: 3,
        quantity: 2,
        initial_cash: 10000,
        fee_rate: 0,
        fixed_fee: 0,
        slippage_pct: 0,
        stop_loss_pct: 0,
      },
      metrics: {
        initial_cash: 10000,
        final_equity: 10200,
        total_pnl: 200,
        total_return_pct: 2,
        max_drawdown_pct: 0,
        trade_count: 2,
        closed_trade_count: 1,
        winning_trades: 1,
        losing_trades: 0,
        win_rate: 100,
        avg_holding_minutes: 1,
        fees_paid: 0,
        skipped_signals: 0,
        final_state: 'flat',
      },
      equity_curve: [
        {
          timestamp: '2026-05-22T10:00:00Z',
          close: 105,
          equity: 10010,
          realized_pnl: 0,
          unrealized_pnl: 10,
          drawdown_pct: 0,
          position: 'long',
        },
        {
          timestamp: '2026-05-22T10:01:00Z',
          close: 200,
          equity: 10200,
          realized_pnl: 200,
          unrealized_pnl: 0,
          drawdown_pct: 0,
          position: 'flat',
        },
      ],
      trades: [
        {
          timestamp: '2026-05-22T10:00:00Z',
          action: 'BUY',
          price: 100,
          quantity: 2,
          fee: 0,
          pnl: 0,
          state_after: 'long',
          reason: 'low reached buy_low',
          holding_minutes: null,
        },
        {
          timestamp: '2026-05-22T10:01:00Z',
          action: 'SELL',
          price: 200,
          quantity: 2,
          fee: 0,
          pnl: 200,
          state_after: 'flat',
          reason: 'exit threshold reached',
          holding_minutes: 1,
        },
      ],
      skipped_signals: [{
        timestamp: '2026-05-22T10:02:00Z',
        action: 'SELL',
        price: 101,
        reason: 'net profit below min_profit_amount',
        state: 'long',
        category: 'FEE',
      }],
      fee_sensitivity: [
        { fee_rate: 0, total_pnl: 200, total_return_pct: 2, max_drawdown_pct: 0 },
        { fee_rate: 0.001, total_pnl: 199.4, total_return_pct: 1.994, max_drawdown_pct: 0 },
      ],
    },
  }).as('runBacktest')

  cy.intercept('POST', '/api/control/start', (req) => {
    status = { ...status, paused: false, kill_switch: false }
    req.reply({ body: { message: 'runner started' } })
  }).as('startAction')

  cy.intercept('POST', '/api/control/stop', (req) => {
    status = { ...status, paused: true }
    req.reply({ body: { message: 'runner stopped' } })
  }).as('stopAction')

  cy.intercept('POST', '/api/control/pause', (req) => {
    status = { ...status, paused: true }
    req.reply({ body: { message: 'trading paused' } })
  }).as('pauseAction')

  cy.intercept('POST', '/api/control/resume', (req) => {
    status = { ...status, paused: false }
    req.reply({ body: { message: 'trading resumed' } })
  }).as('resumeAction')

  cy.intercept('POST', '/api/control/kill-switch', (req) => {
    status = { ...status, kill_switch: true }
    req.reply({ body: { message: 'kill switch activated' } })
  }).as('killSwitchAction')

  cy.intercept('POST', '/api/control/disable-kill-switch', (req) => {
    status = { ...status, kill_switch: false }
    req.reply({ body: { message: 'kill switch disabled' } })
  }).as('disableKillSwitchAction')

  cy.intercept('PUT', '/api/strategy', {
    body: {
      id: 1, symbol: 'AAPL.US', market: 'US', buy_low: 100, sell_high: 200,
      short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
      min_profit_amount: 0,
      auto_resume_minutes: 3,
      llm_interval_minutes: 2,
      trading_session_mode: 'ANY',
      updated_at: '2026-01-01T00:00:00Z',
    },
  }).as('saveStrategy')

  cy.intercept('POST', '/api/strategy/llm-interval/preview', (req) => {
    req.reply({
      body: {
        success: true,
        suggested_buy_low: 155.5,
        suggested_sell_high: 198.8,
        confidence_score: 0.82,
        analysis: '预览分析建议',
        applied: false,
        reason: null,
      },
    })
  }).as('previewLLMInterval')

  cy.intercept('PUT', '/api/credentials', {
    body: {
      id: 1, longbridge_app_key: '', longbridge_app_secret: '',
      longbridge_access_token: '', sct_key: '',
      has_longbridge_app_key: false, has_longbridge_app_secret: false,
      has_longbridge_access_token: false, has_sct_key: false,
      notification_channels: [{ type: 'serverchan', severity_floor: 'INFO' }],
      updated_at: '2026-01-01T00:00:00Z', reload_warning: null,
    },
  }).as('saveCredentials')

  cy.intercept('GET', '/api/strategy-experiments', { body: [] }).as('listStrategyExperiments')
  cy.intercept('GET', '/api/strategy-experiments/*/runs*', {
    body: { items: [], total: 0, page: 1, page_size: 20 },
  }).as('listStrategyExperimentRuns')
})

Cypress.Commands.add('visitApp', (path = '/') => {
  cy.stubApi()
  cy.visit(path)
})

declare global {
  namespace Cypress {
    interface Chainable {
      setupApp: () => Chainable<void>
      stubApi: () => Chainable<void>
      visitApp: (path?: string) => Chainable<void>
    }
  }
}

export {}
