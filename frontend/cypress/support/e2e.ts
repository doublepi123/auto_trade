type EngineState = 'flat' | 'long' | 'short'

interface StatusStub {
  engine_state: EngineState
  paused: boolean
  kill_switch: boolean
  daily_pnl: number
  consecutive_losses: number
  last_price: number
  last_trigger_price: number
  last_trigger_at: string | null
}

function initialStatus(): StatusStub {
  return {
    engine_state: 'flat',
    paused: false,
    kill_switch: false,
    daily_pnl: 0,
    consecutive_losses: 0,
    last_price: 0,
    last_trigger_price: 0,
    last_trigger_at: null,
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
      updated_at: '2026-01-01T00:00:00Z',
    },
  }).as('getStrategy')

  cy.intercept('GET', '/api/status', (req) => {
    req.reply({ body: status })
  }).as('getStatus')

  cy.intercept('GET', '/api/account', {
    body: { total_assets: 0, cash_balances: [], positions: [], available: true, error: null },
  }).as('getAccount')

  cy.intercept('GET', '/api/credentials', {
    body: {
      id: 1, longbridge_app_key: '', longbridge_app_secret: '',
      longbridge_access_token: '', sct_key: '',
      has_longbridge_app_key: false, has_longbridge_app_secret: false,
      has_longbridge_access_token: false, has_sct_key: false,
      updated_at: '2026-01-01T00:00:00Z',
    },
  }).as('getCredentials')

  cy.intercept('GET', '/api/orders*', { body: [] }).as('getOrders')

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
      updated_at: '2026-01-01T00:00:00Z', reload_warning: null,
    },
  }).as('saveCredentials')
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
