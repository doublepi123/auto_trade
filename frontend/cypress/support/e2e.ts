Cypress.Commands.add('setupApp', () => {
  cy.window().then((win) => {
    win.localStorage.setItem('api_key', 'test-api-key')
  })
})

Cypress.Commands.add('stubApi', () => {
  cy.intercept('GET', '/api/strategy', {
    body: {
      id: 1, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
      short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
      updated_at: '2026-01-01T00:00:00Z',
    },
  }).as('getStrategy')

  cy.intercept('GET', '/api/status', {
    body: {
      engine_state: 'flat', paused: false, kill_switch: false,
      daily_pnl: 0, consecutive_losses: 0, last_price: 0,
      last_trigger_price: 0, last_trigger_at: null,
    },
  }).as('getStatus')

  cy.intercept('GET', '/api/account', {
    body: { total_assets: 0, cash_balances: [], positions: [] },
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

  cy.intercept('POST', '/api/control/*', { body: { message: 'ok' } }).as('controlAction')

  cy.intercept('PUT', '/api/strategy', {
    body: {
      id: 1, symbol: 'AAPL.US', market: 'US', buy_low: 100, sell_high: 200,
      short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
      updated_at: '2026-01-01T00:00:00Z',
    },
  }).as('saveStrategy')

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

declare global {
  namespace Cypress {
    interface Chainable {
      setupApp: () => Chainable<void>
      stubApi: () => Chainable<void>
    }
  }
}