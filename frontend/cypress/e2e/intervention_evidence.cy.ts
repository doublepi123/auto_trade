const PAIRING_RULE =
  'Manual pause/resume and kill-switch durations come ONLY from the authoritative audit stream.'

interface EvidenceRowFixture {
  source: 'audit' | 'trade_auto'
  source_id: number
  timestamp: string
  family: 'pause' | 'kill_switch'
  kind: string
  direction: 'open' | 'close'
  reason: string
  action: string
  actor_hash: string | null
  pairing_status: 'PAIRED' | 'OPEN' | 'UNMATCHED_CLOSE' | 'AMBIGUOUS' | 'UNKNOWN'
  paired_source: 'audit' | 'trade_auto' | null
  paired_source_id: number | null
  duration_seconds: number | null
}

function makeRow(overrides: Partial<EvidenceRowFixture>): EvidenceRowFixture {
  return {
    source: 'audit',
    source_id: 1,
    timestamp: '2026-07-20T10:00:00Z',
    family: 'pause',
    kind: 'PAUSE',
    direction: 'open',
    reason: 'MANUAL_PAUSE',
    action: 'PAUSE',
    actor_hash: 'abc12345deadbeef',
    pairing_status: 'OPEN',
    paired_source: null,
    paired_source_id: null,
    duration_seconds: null,
    ...overrides,
  }
}

function makeSummary(overrides: Record<string, number | boolean> = {}) {
  return {
    total_evidence: 6,
    scanned_evidence: 6,
    classification_complete: true,
    paired_count: 2,
    open_count: 1,
    unmatched_close_count: 2,
    ambiguous_count: 1,
    unknown_count: 0,
    paired_duration_seconds: 83,
    ...overrides,
  }
}

function makeResponse(rows: EvidenceRowFixture[], overrides: Record<string, unknown> = {}) {
  return {
    items: rows,
    summary: makeSummary(),
    total: 6,
    pairing_context_scanned: 6,
    filtered_scanned: 6,
    returned: rows.length,
    truncated: false,
    pairing_complete: true,
    scan_truncated: false,
    classification_complete: true,
    pairing_rule: PAIRING_RULE,
    filters: { limit: 500 },
    ...overrides,
  }
}

const MIXED_ROWS: EvidenceRowFixture[] = [
  makeRow({
    source_id: 11,
    timestamp: '2026-07-20T10:00:00Z',
    pairing_status: 'PAIRED',
    paired_source: 'audit',
    paired_source_id: 12,
  }),
  makeRow({
    source_id: 12,
    timestamp: '2026-07-20T10:01:23Z',
    kind: 'RESUME',
    action: 'RESUME',
    direction: 'close',
    reason: 'MANUAL_RESUME',
    pairing_status: 'PAIRED',
    paired_source: 'audit',
    paired_source_id: 11,
    duration_seconds: 83,
  }),
  makeRow({
    source_id: 13,
    timestamp: '2026-07-21T09:00:00Z',
    pairing_status: 'OPEN',
  }),
  makeRow({
    source_id: 14,
    timestamp: '2026-07-19T15:00:00Z',
    kind: 'RESUME',
    action: 'RESUME',
    direction: 'close',
    reason: 'MANUAL_RESUME',
    pairing_status: 'UNMATCHED_CLOSE',
  }),
  makeRow({
    source_id: 15,
    timestamp: '2026-07-18T11:00:00Z',
    family: 'kill_switch',
    kind: 'KILL_SWITCH',
    action: 'KILL_SWITCH',
    reason: 'MANUAL_KILL_SWITCH',
    pairing_status: 'AMBIGUOUS',
  }),
  makeRow({
    source: 'trade_auto',
    source_id: 16,
    timestamp: '2026-07-17T13:30:00Z',
    kind: 'RISK_AUTO_RESUMED',
    action: 'RISK_AUTO_RESUMED',
    direction: 'close',
    reason: 'AUTOMATIC_RESUME',
    actor_hash: null,
    pairing_status: 'UNMATCHED_CLOSE',
  }),
]

function visitTimeline() {
  cy.visit('/#/events')
  cy.get('h3', { timeout: 10000 }).should('contain', '决策时间线')
}

/**
 * Generate `count` contract-valid evidence rows: chronological, alternating
 * PAUSE(open)/RESUME(close). Default mode models a fully-paired universe —
 * explicit open→close PAIRED pairs with `duration_seconds` on close rows
 * only, plus a trailing OPEN row when `count` is odd. `unknown: true` models
 * a scan-capped universe: every context-dependent state degrades to UNKNOWN
 * and all durations are suppressed, matching the backend pairing contract.
 */
function makeGeneratedRows(count: number, opts: { unknown?: boolean } = {}): EvidenceRowFixture[] {
  const rows: EvidenceRowFixture[] = []
  const base = Date.UTC(2026, 6, 20, 10, 0, 0)
  for (let i = 0; i < count; i += 1) {
    const id = 1000 + i
    const isClose = i % 2 === 1
    const trailingOpen = !opts.unknown && !isClose && i === count - 1
    rows.push(
      makeRow({
        source_id: id,
        timestamp: new Date(base + i * 60_000).toISOString(),
        kind: isClose ? 'RESUME' : 'PAUSE',
        action: isClose ? 'RESUME' : 'PAUSE',
        direction: isClose ? 'close' : 'open',
        reason: isClose ? 'MANUAL_RESUME' : 'MANUAL_PAUSE',
        pairing_status: opts.unknown ? 'UNKNOWN' : trailingOpen ? 'OPEN' : 'PAIRED',
        paired_source: !opts.unknown && !trailingOpen ? 'audit' : null,
        paired_source_id: !opts.unknown && !trailingOpen ? (isClose ? id - 1 : id + 1) : null,
        duration_seconds: !opts.unknown && isClose ? 60 : null,
      }),
    )
  }
  return rows
}

describe('Decision Timeline intervention evidence', () => {
  it('renders paired, open, unmatched-close and ambiguous rows with full context', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/intervention-evidence*', { body: makeResponse(MIXED_ROWS) }).as(
      'evidenceMixed',
    )

    visitTimeline()
    cy.wait('@evidenceMixed')

    // Metadata denominators, faithfully labelled.
    cy.get('[data-testid="evidence-returned-tag"]').should('contain', '返回 6 / 6 条')
    cy.get('[data-testid="evidence-summary"]').should('contain', '证据总数 6')
    cy.get('[data-testid="evidence-summary"]').should('contain', '筛选后已扫描 6')
    cy.get('[data-testid="evidence-summary"]').should('contain', '配对上下文扫描 6')
    cy.get('[data-testid="evidence-filters-echo"]').should('contain', '未设置日期筛选 · 上限 500')

    // Per-status counts including unknown/ambiguous; 已配对证据 counts rows, not pairs.
    cy.get('[data-testid="evidence-counts"]').should('contain', '已配对证据 2')
    cy.get('[data-testid="evidence-counts"]').should('contain', '未关闭 1')
    cy.get('[data-testid="evidence-counts"]').should('contain', '无配对关闭 2')
    cy.get('[data-testid="evidence-counts"]').should('contain', '歧义 1')
    cy.get('[data-testid="evidence-counts"]').should('contain', '未知 0')
    cy.get('[data-testid="evidence-paired-duration"]').should('contain', '1 分 23 秒')
    cy.get('[data-testid="evidence-counts-note"]').should('contain', '已扫描且符合筛选的 6 条证据')

    // Row-level pairing states.
    cy.get('[data-testid="evidence-status-tag"]').filter(':contains("已配对")').should('have.length', 2)
    cy.get('[data-testid="evidence-status-tag"]').filter(':contains("未关闭")').should('have.length', 1)
    cy.get('[data-testid="evidence-status-tag"]').filter(':contains("无配对关闭")').should('have.length', 2)
    cy.get('[data-testid="evidence-status-tag"]').filter(':contains("歧义")').should('have.length', 1)

    // Duration appears only on the paired close row; every other row is honest '—'.
    cy.get('[data-testid="evidence-duration"]').filter(':contains("1 分 23 秒")').should('have.length', 1)
    cy.get('[data-testid="evidence-duration"]').filter(':contains("—")').should('have.length', 5)

    // Fixed reason codes, action labels, families, sources, actor.
    cy.get('[data-testid="evidence-table"]').should('contain', 'MANUAL_PAUSE')
    cy.get('[data-testid="evidence-table"]').should('contain', 'MANUAL_RESUME')
    cy.get('[data-testid="evidence-table"]').should('contain', 'MANUAL_KILL_SWITCH')
    cy.get('[data-testid="evidence-table"]').should('contain', 'AUTOMATIC_RESUME')
    cy.get('[data-testid="evidence-table"]').should('contain', '自动恢复')
    cy.get('[data-testid="evidence-table"]').should('contain', '紧急停止')
    cy.get('[data-testid="evidence-table"]').should('contain', 'abc12345')
    cy.get('[data-testid="evidence-rule"]').should('contain', '配对规则说明')

    // The raw timeline below is preserved, not replaced.
    cy.get('.timeline-table').should('exist')
    cy.get('[data-testid="timeline-source-filter"]').should('be.visible')
  })

  it('renders scan-cap and response-limit truncation alerts independently when both apply', () => {
    // Contract-consistent combined case driven through the real UI flow: the
    // global scan cap is exceeded (pairing_context_scanned 5001 > 5000 →
    // pairing incomplete → all rows UNKNOWN, durations suppressed, precise
    // total kept) AND the user-selected limit=100 cuts the unfiltered scanned
    // population (filtered_scanned 5001 → returned 100). The stub answers
    // every request the way the backend would for this universe.
    const queries: Array<Record<string, string>> = []
    cy.stubApi()
    cy.intercept('GET', '/api/intervention-evidence*', (req) => {
      queries.push(req.query as Record<string, string>)
      const limit = Number(req.query.limit ?? 500)
      const rows = makeGeneratedRows(Math.min(limit, 5001), { unknown: true })
      req.reply({
        body: makeResponse(rows, {
          summary: makeSummary({
            total_evidence: 5300,
            scanned_evidence: 5001,
            classification_complete: false,
            paired_count: 0,
            open_count: 0,
            unmatched_close_count: 0,
            ambiguous_count: 0,
            unknown_count: 5001,
            paired_duration_seconds: 0,
          }),
          total: 5300,
          pairing_context_scanned: 5001,
          filtered_scanned: 5001,
          returned: rows.length,
          truncated: true,
          pairing_complete: false,
          scan_truncated: true,
          classification_complete: false,
          filters: { limit },
        }),
      })
    }).as('evidenceCombined')

    visitTimeline()
    cy.wait('@evidenceCombined')

    // Drive limit=100 through the actual select — the query flow, not a
    // fabricated filters echo.
    cy.get('[data-testid="evidence-limit"]').click()
    cy.contains('.el-select-dropdown__item', '上限 100').click()
    cy.wait('@evidenceCombined')

    cy.wrap(null).should(() => {
      expect(queries[queries.length - 1].limit).to.equal('100')
    })
    cy.get('[data-testid="evidence-incomplete-alert"]').should('contain', '配对不完整')
    cy.get('[data-testid="evidence-incomplete-alert"]').should('contain', '时长已抑制')
    cy.get('[data-testid="evidence-incomplete-alert"]').should('contain', '总数 5300 条仍为精确值')
    cy.get('[data-testid="evidence-truncated-alert"]').should('contain', '仅显示前 100 条（符合筛选共 5300 条）')
    cy.get('[data-testid="evidence-returned-tag"]').should('contain', '返回 100 / 5300 条')
    cy.get('[data-testid="evidence-filters-echo"]').should('contain', '未设置日期筛选 · 上限 100')
    // Summary counts describe the scanned population (5001), not just returned rows.
    cy.get('[data-testid="evidence-counts"]').should('contain', '未知 5001')
    cy.get('[data-testid="evidence-status-tag"]').filter(':contains("未知")').should('have.length', 100)
    // No fabricated durations when pairing is incomplete.
    cy.get('[data-testid="evidence-duration"]').each(($cell) => {
      expect($cell.text().trim()).to.equal('—')
    })
    cy.get('[data-testid="evidence-paired-duration"]').should('contain', '—（已抑制）')
    cy.get('[data-testid="evidence-counts-note"]').should('contain', '配对不完整时仅描述该部分')
  })

  it('shows only the scan-cap alert when date filters shrink the result below the limit', () => {
    // Global pairing context is scan-capped (5001) so every row is UNKNOWN,
    // but the UI-driven date filters shrink the matching population to 2 —
    // below the driven limit=100, so no response-limit truncation occurs.
    // The stub mirrors backend semantics for both unfiltered and filtered
    // requests and echoes the filters it actually received.
    const queries: Array<Record<string, string>> = []
    cy.stubApi()
    cy.intercept('GET', '/api/intervention-evidence*', (req) => {
      queries.push(req.query as Record<string, string>)
      const limit = Number(req.query.limit ?? 500)
      const from = typeof req.query.from_date === 'string' ? req.query.from_date : ''
      const to = typeof req.query.to_date === 'string' ? req.query.to_date : ''
      const filters: Record<string, unknown> = { limit }
      if (from) filters.from_date = from
      if (to) filters.to_date = to
      const dateFiltered = Boolean(from || to)
      const scanned = dateFiltered ? 2 : 5001
      const rows = makeGeneratedRows(Math.min(limit, scanned), { unknown: true })
      req.reply({
        body: makeResponse(rows, {
          summary: makeSummary({
            total_evidence: dateFiltered ? 2 : 5300,
            scanned_evidence: scanned,
            classification_complete: false,
            paired_count: 0,
            open_count: 0,
            unmatched_close_count: 0,
            ambiguous_count: 0,
            unknown_count: scanned,
            paired_duration_seconds: 0,
          }),
          total: dateFiltered ? 2 : 5300,
          pairing_context_scanned: 5001,
          filtered_scanned: scanned,
          returned: rows.length,
          truncated: true,
          pairing_complete: false,
          scan_truncated: true,
          classification_complete: false,
          filters,
        }),
      })
    }).as('evidenceScanCapOnly')

    visitTimeline()
    cy.wait('@evidenceScanCapOnly')

    cy.get('[data-testid="evidence-from-date"] input').type('2026-07-01{enter}')
    cy.get('[data-testid="evidence-to-date"] input').type('2026-07-31{enter}')
    cy.get('[data-testid="evidence-limit"]').click()
    cy.contains('.el-select-dropdown__item', '上限 100').click()

    cy.wrap(null).should(() => {
      const last = queries[queries.length - 1]
      expect(last.from_date).to.equal('2026-07-01')
      expect(last.to_date).to.equal('2026-07-31')
      expect(last.limit).to.equal('100')
    })
    cy.get('[data-testid="evidence-filters-echo"]').should('contain', '筛选 2026-07-01 至 2026-07-31 · 上限 100')
    cy.get('[data-testid="evidence-incomplete-alert"]').should('contain', '配对不完整')
    cy.get('[data-testid="evidence-truncated-alert"]').should('not.exist')
    cy.get('[data-testid="evidence-returned-tag"]').should('contain', '返回 2 / 2 条')
    cy.get('[data-testid="evidence-status-tag"]').filter(':contains("未知")').should('have.length', 2)
    cy.get('[data-testid="evidence-duration"]').each(($cell) => {
      expect($cell.text().trim()).to.equal('—')
    })
  })

  it('shows only the response-limit alert when the scan is complete', () => {
    // 101 evidence rows, fully scanned (well below the 5000 cap): only the
    // user-driven limit=100 truncates the response. The stub honors the
    // requested limit exactly like the bounded backend query.
    const allRows = makeGeneratedRows(101)
    const queries: Array<Record<string, string>> = []
    cy.stubApi()
    cy.intercept('GET', '/api/intervention-evidence*', (req) => {
      queries.push(req.query as Record<string, string>)
      const limit = Number(req.query.limit ?? 500)
      const rows = allRows.slice(0, Math.min(limit, allRows.length))
      req.reply({
        body: makeResponse(rows, {
          summary: makeSummary({
            total_evidence: 101,
            scanned_evidence: 101,
            classification_complete: true,
            paired_count: 100,
            open_count: 1,
            unmatched_close_count: 0,
            ambiguous_count: 0,
            unknown_count: 0,
            paired_duration_seconds: 3000,
          }),
          total: 101,
          pairing_context_scanned: 101,
          filtered_scanned: 101,
          returned: rows.length,
          truncated: rows.length < 101,
          pairing_complete: true,
          scan_truncated: false,
          classification_complete: true,
          filters: { limit },
        }),
      })
    }).as('evidenceTruncated')

    visitTimeline()
    cy.wait('@evidenceTruncated')
    // Default limit 500 returns the full population: no truncation at all.
    cy.get('[data-testid="evidence-truncated-alert"]').should('not.exist')

    cy.get('[data-testid="evidence-limit"]').click()
    cy.contains('.el-select-dropdown__item', '上限 100').click()
    cy.wait('@evidenceTruncated')

    cy.wrap(null).should(() => {
      expect(queries[queries.length - 1].limit).to.equal('100')
    })
    cy.get('[data-testid="evidence-truncated-alert"]').should('contain', '仅显示前 100 条（符合筛选共 101 条）')
    cy.get('[data-testid="evidence-incomplete-alert"]').should('not.exist')
    cy.get('[data-testid="evidence-returned-tag"]').should('contain', '返回 100 / 101 条')
    cy.get('[data-testid="evidence-filters-echo"]').should('contain', '未设置日期筛选 · 上限 100')
    // Counts classify the full scanned population (101), not just returned rows.
    cy.get('[data-testid="evidence-counts"]').should('contain', '已配对证据 100')
    cy.get('[data-testid="evidence-counts"]').should('contain', '未关闭 1')
    cy.get('[data-testid="evidence-paired-duration"]').should('contain', '50 分')
    cy.get('[data-testid="evidence-status-tag"]').should('have.length', 100)
  })

  it('shows an empty state with honest zero metadata', () => {
    cy.stubApi()
    visitTimeline()
    cy.wait('@getInterventionEvidence')

    cy.get('[data-testid="evidence-empty"]').should('contain', '当前条件下暂无干预证据')
    cy.get('[data-testid="evidence-summary"]').should('contain', '证据总数 0')
    cy.get('[data-testid="evidence-table"]').should('not.exist')
  })

  it('shows an error state and recovers through the GET-only retry', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/intervention-evidence*', {
      statusCode: 503,
      body: { detail: 'intervention evidence snapshot unavailable: caller session cannot be given a distinct physical read snapshot' },
    }).as('evidenceError')

    visitTimeline()
    cy.wait('@evidenceError')
    cy.get('[data-testid="evidence-error"]').should('contain', 'snapshot unavailable')

    cy.intercept('GET', '/api/intervention-evidence*', { body: makeResponse(MIXED_ROWS) }).as(
      'evidenceRetry',
    )
    cy.get('[data-testid="evidence-retry"]').click()
    cy.wait('@evidenceRetry')
    cy.get('[data-testid="evidence-error"]').should('not.exist')
    cy.get('[data-testid="evidence-table"]').should('contain', 'MANUAL_PAUSE')
  })

  it('sends date filters and limit as GET query parameters and echoes them', () => {
    const seen: Array<Record<string, string>> = []
    cy.stubApi()
    cy.intercept('GET', '/api/intervention-evidence*', (req) => {
      seen.push(req.query as Record<string, string>)
      req.reply({
        body: makeResponse(MIXED_ROWS.slice(0, 1), {
          summary: makeSummary({ total_evidence: 1, scanned_evidence: 1, paired_count: 1, open_count: 0, unmatched_close_count: 0, ambiguous_count: 0 }),
          total: 1,
          filtered_scanned: 1,
          returned: 1,
          filters: { limit: 1000, from_date: '2026-07-01', to_date: '2026-07-31' },
        }),
      })
    }).as('evidenceFiltered')

    visitTimeline()
    cy.wait('@evidenceFiltered')

    cy.get('[data-testid="evidence-from-date"] input').type('2026-07-01{enter}')
    cy.get('[data-testid="evidence-to-date"] input').type('2026-07-31{enter}')
    cy.get('[data-testid="evidence-limit"]').click()
    cy.contains('.el-select-dropdown__item', '上限 1000').click()

    // Retry-until: the final request (limit change) may still be in flight.
    cy.wrap(null).should(() => {
      expect(seen.length).to.be.greaterThan(1)
      const last = seen[seen.length - 1]
      expect(last.from_date).to.equal('2026-07-01')
      expect(last.to_date).to.equal('2026-07-31')
      expect(last.limit).to.equal('1000')
    })
    cy.get('[data-testid="evidence-filters-echo"]').should('contain', '筛选 2026-07-01 至 2026-07-31 · 上限 1000')
  })

  it('surfaces the backend 422 detail for an inverted date range', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/intervention-evidence*', (req) => {
      const { from_date: from, to_date: to } = req.query
      if (from && to && from > to) {
        req.reply({ statusCode: 422, body: { detail: 'from_date must be on or before to_date' } })
        return
      }
      req.reply({ body: makeResponse([]) })
    }).as('evidence422')

    visitTimeline()
    cy.wait('@evidence422')

    cy.get('[data-testid="evidence-from-date"] input').type('2026-07-31{enter}')
    cy.get('[data-testid="evidence-to-date"] input').type('2026-07-01{enter}')
    cy.wait('@evidence422')
    cy.get('[data-testid="evidence-error"]').should('contain', 'from_date must be on or before to_date')
  })

  it('reloads evidence through the existing timeline refresh button', () => {
    cy.stubApi()
    visitTimeline()
    cy.wait('@getInterventionEvidence')
    cy.get('@getInterventionEvidence.all').should('have.length', 1)

    // The one existing 刷新 button reloads both surfaces; no new control exists.
    cy.get('[data-testid="timeline-refresh"]').click()
    cy.wait('@getInterventionEvidence')
    cy.get('@getInterventionEvidence.all').should('have.length', 2)
    cy.get('@getEvents.all').should('have.length.greaterThan', 1)
    cy.get('[data-testid="intervention-evidence-panel"]').within(() => {
      cy.contains('button', '刷新').should('not.exist')
    })
  })

  it('offers no intervention controls and never calls mutating endpoints', () => {
    let mutatingCalls = 0
    cy.stubApi()
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
      cy.intercept(method, '/api/**', (req) => {
        mutatingCalls += 1
        req.reply({ statusCode: 404, body: {} })
      })
    }

    visitTimeline()
    cy.wait('@getInterventionEvidence')

    cy.get('[data-testid="intervention-evidence-panel"]').within(() => {
      cy.contains('button', '暂停').should('not.exist')
      cy.contains('button', '恢复').should('not.exist')
      cy.contains('button', '紧急停止').should('not.exist')
      cy.contains('button', '解除').should('not.exist')
      cy.contains('button', '启动').should('not.exist')
    })

    // Filtering is a GET-only interaction too.
    cy.get('[data-testid="evidence-limit"]').click()
    cy.contains('.el-select-dropdown__item', '上限 100').click()
    cy.wait('@getInterventionEvidence')
    cy.wrap(null).then(() => {
      expect(mutatingCalls).to.equal(0)
    })
  })

  it('keeps the panel usable on a mobile viewport', () => {
    cy.viewport(390, 844)
    cy.stubApi()
    cy.intercept('GET', '/api/intervention-evidence*', { body: makeResponse(MIXED_ROWS) }).as(
      'evidenceMobile',
    )

    visitTimeline()
    cy.wait('@evidenceMobile')
    cy.get('[data-testid="intervention-evidence-panel"]').should('be.visible')
    cy.get('[data-testid="evidence-counts"]').should('contain', '已配对证据 2')
    cy.get('body').then(($body) => {
      expect($body[0].scrollWidth).to.be.lte($body[0].clientWidth)
    })
  })
})
