# Dashboard and Configuration Performance Design

## Context

Dashboard and configuration pages feel sluggish because the UI waits too broadly for API responses and several backend endpoints perform slow work synchronously. The goal is to improve both perceived responsiveness and real API latency without changing the product scope or redesigning the application.

Relevant pages:

- `frontend/src/views/Dashboard.vue`
- `frontend/src/views/Strategy.vue`
- `frontend/src/views/Credentials.vue`

Relevant frontend data paths:

- `frontend/src/composables/useDashboardData.ts`
- `frontend/src/composables/useStatusStream.ts`
- `frontend/src/composables/useAccountRefresh.ts`
- `frontend/src/composables/useFormState.ts`

Relevant backend endpoints:

- `GET /api/status`
- `GET /api/strategy`
- `GET /api/account`
- `GET /api/credentials`
- `GET /api/strategy/llm-interval/status`
- `PUT /api/strategy`
- `PUT /api/credentials`

## Findings

### Frontend

Dashboard currently uses a page-level loading overlay for initial dashboard data. `useDashboardData.load()` waits for both strategy and status. The page also starts account refresh, LLM status loading, websocket connection, and polling during mount. One slow request can therefore make the dashboard feel blocked even when other sections could render.

Status polling and account refresh do not guard against an already running request. If the backend is slow, repeated intervals can stack, adding pressure to the API and creating stale response races.

Strategy and credentials pages render forms while data is loading, but they do not provide a clear progressive loading state. This can feel like a frozen or incorrect page when the API is slow.

### Backend

`GET /api/account` is the largest latency risk. It synchronously calls broker account, broker positions, and one quote request per position. Dashboard polls this endpoint every 10 seconds, so slow broker calls directly affect user experience and backend load.

LLM analyze and preview are expected to be slow because they call external services. They should not block normal page rendering. LLM status is DB-only and should remain lightweight.

Credential save and strategy save trigger runner reload side effects. These operations can be slow if they touch broker state or subscriptions. They should not keep the user waiting for non-critical live reload work when the persisted save has already succeeded.

## Approaches Considered

### Approach A: UI-only progressive loading

Replace broad page-level loading with section-level skeletons and clearer loading states. Add stale-data display where possible.

Pros:

- Smallest backend risk.
- Fastest visible improvement.
- Keeps current data contracts.

Cons:

- Does not reduce broker/API load.
- Slow `/api/account` can still consume backend capacity.

### Approach B: Backend-only latency reduction

Add caching, in-flight request protection, and async reload behavior for the slow endpoints.

Pros:

- Reduces real latency and backend pressure.
- Helps all consumers, not only the current UI.

Cons:

- UI may still feel blocked during unavoidable slow calls.
- More backend test coverage is required.

### Approach C: Combined incremental optimization

Apply section-level UI loading and targeted backend latency reduction together. Keep changes small and focused.

Pros:

- Improves perceived responsiveness and real latency.
- Addresses both root causes found during diagnosis.
- Keeps the existing Element Plus admin UI style.

Cons:

- Touches both frontend and backend.
- Requires frontend Cypress coverage plus backend unit tests.

Recommendation: Approach C.

## Design

### Dashboard UI

The dashboard should render above-the-fold status and controls as soon as status is available. Account, strategy summary, and LLM status should load independently with card-level loading states.

Changes:

- Replace the full-dashboard `v-loading` dependency with section-level loading flags.
- Split dashboard bootstrap into independent status, strategy, account, and LLM status loads.
- Show skeleton or muted placeholder content inside account and LLM cards while their requests are pending.
- Preserve existing values during refresh instead of clearing the card.
- Keep an explicit retry action when core status/strategy loading fails.

Success criteria:

- A slow account request does not block engine state, price, PnL, and control cards from rendering.
- A slow LLM status request does not block the dashboard.
- Account refresh shows stale data plus a small refreshing indicator instead of an empty page.

### Dashboard polling

Status and account refresh should not create overlapping requests.

Changes:

- Add an in-flight guard to account refresh.
- Add an in-flight guard to status polling fallback.
- Keep websocket as the preferred live status path.
- Poll status only when websocket data is stale or websocket is disconnected.
- Avoid applying stale older responses over newer websocket data where feasible.

Success criteria:

- At most one account refresh is running at a time per dashboard instance.
- At most one status fallback poll is running at a time per dashboard instance.
- When websocket updates are fresh, status polling does not create unnecessary API pressure.

### Strategy configuration UI

The strategy page should clearly distinguish initial loading from editable loaded state.

Changes:

- Add a form-level skeleton or loading wrapper for initial strategy load.
- Disable editable inputs until the initial strategy load completes.
- Keep LLM status loading independent from the strategy form.
- Keep long-running LLM analyze/preview as explicit user-triggered actions with button loading and clear status text.

Success criteria:

- Users do not see editable default strategy values before real data arrives.
- Slow LLM status does not block the strategy form.
- LLM analyze/preview loading is clearly scoped to its own button/card.

### Credentials configuration UI

The credentials page should clearly show that saved credential flags are loading.

Changes:

- Add a card-level loading state for initial credential status load.
- Disable inputs until credential flags are loaded.
- Preserve the existing explanation that secret values are never echoed.
- Keep save feedback and reload warning behavior.

Success criteria:

- Users do not interact with the credentials form before saved credential status is known.
- Slow credential status loading is visible and understandable.

### Account API latency

`GET /api/account` should return quickly during repeated dashboard refreshes and avoid duplicate broker work.

Changes:

- Introduce a short-lived account snapshot cache in the backend account path or a small service used by the endpoint.
- Cache successful account responses for a short TTL suitable for dashboard display, such as 5 seconds.
- If a broker refresh is already in progress, return the latest cached snapshot when available.
- If there is no cache, perform the broker refresh as today.
- Keep the response shape unchanged.
- Mark unavailable only when there is no usable cache and broker retrieval fails.

Success criteria:

- Repeated dashboard polling does not trigger full broker account/positions/quote work every time within the TTL.
- Existing `/api/account` consumers receive the same schema.
- Broker failures can still surface when no cached data exists.

### Save reload latency

Persisting strategy or credentials should be decoupled from non-critical live reload work where possible.

Changes:

- Keep strategy save and credential save persistence synchronous.
- Run runner reload work after save outside the request path when safe.
- Return a warning only when the persisted save succeeds but live reload fails asynchronously or is known to require restart.
- Do not hide persistence failures.

Success criteria:

- Save requests return after persistence instead of waiting on slow broker/subscription reload work.
- Existing validation and response schemas remain compatible.

### Instrumentation

Add lightweight timing logs around slow paths so future regressions can be diagnosed.

Targets:

- `/api/account`
- strategy save reload path
- credential save reload path
- LLM analyze/preview external calls

Success criteria:

- Logs identify endpoint duration and major sub-step duration without exposing credentials or sensitive data.

## Testing

Frontend tests:

- Dashboard renders status/control sections even when account API is delayed.
- Dashboard account card shows loading/stale state during refresh.
- Polling does not issue overlapping account requests under delayed responses.
- Strategy form is not editable before initial load completes.
- Credentials form is not editable before credential flags load.

Backend tests:

- `/api/account` uses cached snapshot within TTL.
- `/api/account` returns cached data when a refresh fails and cache is available.
- `/api/account` preserves unavailable response when no cache exists and broker calls fail.
- Credential save persists even if live reload fails and reports reload warning behavior.
- Strategy save keeps existing validation and does not block on slow live reload when runner is active.

Manual verification:

- Run frontend dev server and backend API.
- Simulate slow `/api/account` and confirm dashboard status/control cards render promptly.
- Confirm account data refreshes without overlapping requests.
- Confirm strategy and credentials pages show clear initial loading states.

## Non-Goals

- No visual redesign of the application.
- No changes to trading logic.
- No changes to public API response schemas unless explicitly required later.
- No changes to authentication or credential storage semantics.
- No background job framework or queue system.
