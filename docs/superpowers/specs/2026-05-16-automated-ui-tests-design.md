# Automated UI & API Tests Design

## Goal

Automated tests covering core backend APIs and frontend UI flows to ensure critical functionality works correctly after changes.

## Approach

**Backend**: Extend existing pytest suite with integration tests for all core API endpoints.

**Frontend**: Add Cypress E2E tests that run against the live dev server, testing user-visible flows.

## Backend API Tests

Add to `backend/tests/test_api.py` (or new files as needed):

- `GET /api/health` — returns ok
- `GET /api/strategy` — returns default strategy
- `PUT /api/strategy` — creates/updates strategy, validates fields
- `GET /api/status` — returns runtime state
- `GET /api/account` — returns account data structure
- `GET /api/orders` — returns orders list
- `POST /api/control/start` — starts runner
- `POST /api/control/stop` — stops runner
- `POST /api/control/pause` — pauses trading
- `POST /api/control/resume` — resumes trading
- `POST /api/control/kill-switch` — activates kill switch
- `POST /api/control/disable-kill-switch` — disables kill switch

All tested via FastAPI TestClient, no external dependencies.

## Frontend E2E Tests (Cypress)

### Setup

- Install Cypress in `frontend/`
- Add `npm run cypress:open` and `npm run cypress:run` scripts
- Configure `cypress.config.ts` to proxy API calls to backend

### Test Cases

1. **Dashboard loads** — visit Dashboard, see engine state, price, PnL cards
2. **Account info displays** — visit Dashboard, see total assets card, cash balances table, positions table
3. **Strategy page** — visit Strategy, see form, save valid config
4. **Strategy validation** — try invalid market, inverted buy/sell prices
5. **Control buttons** — start, pause, resume, stop trading
6. **Trade history** — visit History page, see orders table
7. **Credentials page** — visit Credentials, see form
8. **API key dialog** — set API key, verify it's stored

## File Structure

```
frontend/
  cypress/
    e2e/
      dashboard.cy.ts
      strategy.cy.ts
      controls.cy.ts
      history.cy.ts
      credentials.cy.ts
    support/
      e2e.ts
    fixtures/
      strategy.json
  cypress.config.ts
backend/
  tests/
    test_api.py          (existing, extend)
    test_account_api.py  (existing, already has tests)
```