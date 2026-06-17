# P79–P88 Alerts & Notifications Observability Design

## Goal

Deliver 10 autonomous, low-risk iterations that improve operator visibility into notifications and alert firings without touching broker, order, runner, or risk write paths.

## Scope

- **P79–P83 Notification Center polish**: make `/notifications` useful as an operations inbox using data already returned by `GET /api/notifications`.
- **P84–P88 Alert firing observability**: make `/alerts` show recent firing context and per-rule history summaries using existing alert rule data and existing history API.

## Non-goals

- No backend API changes.
- No new database tables or migrations.
- No notification resend, acknowledgement, muting, or deletion workflow.
- No changes to alert evaluation, notification delivery, trading, broker, order, or risk logic.
- No WebSocket or polling loop.

## P79–P83: Notification Center

| Iteration | Feature | Behavior |
|---|---|---|
| P79 | Severity summary | Show total, success, failure, INFO, WARNING, CRITICAL counts for the current page. |
| P80 | Search | Client-side keyword filter across title/content/error/severity. |
| P81 | Quick filters | Chips for all, failures, critical, warning, info; chips combine with search. |
| P82 | Day grouping | Show current filtered notifications grouped by local calendar day above the table. |
| P83 | Empty/result context | Show explicit empty states and result text for current filter/search/page. |

## P84–P88: Alert Firings

| Iteration | Feature | Behavior |
|---|---|---|
| P84 | Rule health cards | Show total, enabled, disabled, recently fired, and never-fired rule counts. |
| P85 | Rule filters | Client-side filter chips for enabled, disabled, recently fired, and never fired. |
| P86 | Recent firing summary | Show top recent `last_fired_at` rules with symbol/severity/time. |
| P87 | History dialog summary | In each rule history dialog, show count, latest trigger value, average trigger value, max trigger value. |
| P88 | History severity/message polish | Add severity counts and message list context in the history dialog while preserving existing table. |

## Data Flow

- Notification Center loads one server page through existing `getNotifications({ page, page_size, severity })`.
- P79–P83 are computed from the current page only. This avoids misleading full-dataset claims because the backend endpoint returns paginated rows, not aggregate counts.
- Alerts page loads existing rules through `listAlertRules()` and per-rule history through `getAlertRuleHistory(ruleId, { limit: 100 })`.
- P84–P88 are computed in Vue from currently loaded rule rows or current dialog history rows.

## UI Placement

- Notification enhancements live in `frontend/src/views/NotificationCenter.vue` above the existing table.
- Alert enhancements live in `frontend/src/views/AlertRules.vue` above the rule table and inside the existing history dialog.
- Existing Cypress `data-testid`s remain stable.

## Testing

- Add Cypress tests before implementation:
  - `frontend/cypress/e2e/notification_center.cy.ts` covers P79–P83.
  - `frontend/cypress/e2e/alert_firings.cy.ts` covers P84–P88.
- Use deterministic `cy.intercept()` data and verify RED before implementation.
- Run `npm run type-check`, `npm run build`, focused Cypress specs, and `git diff --check` after implementation.

## Risks and Mitigations

- **Paginated totals ambiguity**: label notification aggregates as current page / current result, not global counts.
- **Large Vue files**: keep additions computed-only and simple; if files grow further, extract panels later, but avoid an unrelated refactor in this batch.
- **Filter confusion**: quick filters are client-side and combine with search; severity dropdown remains server-side.
