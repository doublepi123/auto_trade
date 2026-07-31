type AnalyticsPanel = {
  route: string
  endpoint: string
  action: string
}

const panels: AnalyticsPanel[] = [
  { route: 'holding-time', endpoint: '/api/holding-time/analyze', action: '分析' },
  { route: 'distribution-shape', endpoint: '/api/distribution-shape/analyze', action: '分析' },
  { route: 'trade-frequency', endpoint: '/api/trade-frequency/analyze', action: '分析' },
  { route: 'profit-factor', endpoint: '/api/profit-factor/decompose', action: '分解' },
  { route: 'concentration', endpoint: '/api/concentration/analyze', action: '分析' },
  { route: 'autocorrelation', endpoint: '/api/autocorrelation/analyze', action: '分析' },
  { route: 'size-impact', endpoint: '/api/size-impact/analyze', action: '分析' },
  { route: 'return-calendar', endpoint: '/api/return-calendar/compute', action: '计算' },
  { route: 'edge-quality', endpoint: '/api/edge-quality/score', action: '评分' },
  { route: 'decay-detection', endpoint: '/api/decay-detection/detect', action: '检测' },
  { route: 'rolling-var', endpoint: '/api/rolling-var/compute', action: '计算' },
  { route: 'asymmetry', endpoint: '/api/asymmetry/analyze', action: '分析' },
  { route: 'capital-efficiency', endpoint: '/api/capital-efficiency/analyze', action: '分析' },
  { route: 'intraday-seasonality', endpoint: '/api/intraday-seasonality/analyze', action: '分析' },
  { route: 'drawdown-duration', endpoint: '/api/drawdown-duration/analyze', action: '分析' },
  { route: 'prediction-score', endpoint: '/api/prediction-score/analyze', action: '分析' },
  { route: 'regime-sensitivity', endpoint: '/api/regime-sensitivity/analyze', action: '分析' },
  { route: 'robustness', endpoint: '/api/robustness/score', action: '评分' },
  { route: 'milestones', endpoint: '/api/milestones/track', action: '追踪' },
  { route: 'momentum-ranking', endpoint: '/api/momentum-ranking/rank', action: '排名' },
  { route: 'fee-drag', endpoint: '/api/fee-drag/summary', action: '分析' },
  { route: 'exit-efficiency', endpoint: '/api/exit-efficiency/summary', action: '分析' },
  { route: 'r-multiples', endpoint: '/api/r-multiples/distribution', action: '分析' },
  { route: 'profit-concentration', endpoint: '/api/profit-concentration/summary', action: '分析' },
  { route: 'scratch-analysis', endpoint: '/api/scratch-analysis/summary', action: '分析' },
  { route: 'reentry-analysis', endpoint: '/api/reentry-analysis/summary', action: '分析' },
  { route: 'first-trade', endpoint: '/api/first-trade/summary', action: '分析' },
  { route: 'loss-containment', endpoint: '/api/loss-containment/summary', action: '分析' },
  { route: 'daily-consistency', endpoint: '/api/daily-consistency/summary', action: '分析' },
]

describe('Analytics panels expose FIFO evidence quality', () => {
  beforeEach(() => {
    cy.stubApi()
  })

  for (const panel of panels) {
    it(`${panel.route} surfaces unresolved ledger evidence`, () => {
      cy.intercept('GET', `${panel.endpoint}*`, {
        body: {
          symbol: 'ALL',
          lookback_days: 180,
          days: 90,
          sample_size: 0,
          error: 'Insufficient quality-gated samples.',
          currency: 'USD',
          currencies: ['USD'],
          totals_comparable: true,
          statistics_quality: {
            status: 'UNRESOLVED',
            known_exclusion_count: 0,
            unresolved_issue_count: 1,
            omitted_day_count: 1,
            items: [{
              trade_day: '2026-07-30',
              symbol: 'AAPL.US',
              issue_code: 'INVALID_FILL_EVIDENCE',
              exit_order_id: 7,
              broker_order_id: 'bad-fill-7',
              side: 'BUY',
              filled_quantity: 0,
              matched_quantity: 0,
              unmatched_quantity: 0,
              exclusion_id: null,
              reason: 'claimed fill has malformed execution evidence',
            }],
          },
        },
      }).as('analyticsEvidence')

      cy.visit(`/#/${panel.route}`)
      cy.contains('button', panel.action).click()
      cy.wait('@analyticsEvidence')
      cy.get('[data-testid="statistics-quality-alert"]')
        .should('have.attr', 'data-quality-status', 'UNRESOLVED')
        .and('contain', '已排除 1 个交易日')
    })
  }
})

describe('Exit efficiency exposes independent excursion coverage', () => {
  beforeEach(() => {
    cy.stubApi()
  })

  it('fails visibly when closed trades lack verified interior snapshots', () => {
    cy.intercept('GET', '/api/exit-efficiency/summary*', {
      body: {
        days: 90,
        sample_size: 0,
        closed_trade_count: 4,
        eligible_excursion_count: 0,
        capture_sample_size: 0,
        mae_sample_size: 0,
        error: 'Need at least 3 closed trades with verified interior snapshot excursion evidence.',
        currency: 'USD',
        currencies: ['USD'],
        totals_comparable: true,
        excursion_quality: {
          status: 'INSUFFICIENT',
          closed_trade_count: 4,
          eligible_excursion_count: 0,
          excluded_excursion_count: 4,
          excluded_by_reason: { ENDPOINT_ONLY: 4 },
          interior_observation_count: 0,
          max_gap_seconds: null,
        },
        statistics_quality: {
          status: 'COMPLETE',
          known_exclusion_count: 0,
          unresolved_issue_count: 0,
          omitted_day_count: 0,
          items: [],
        },
      },
    }).as('excursionEvidence')

    cy.visit('/#/exit-efficiency')
    cy.contains('button', '分析').click()
    cy.wait('@excursionEvidence')
    cy.get('[data-testid="excursion-quality-alert"]')
      .should('contain', '0/4 笔可用')
  })
})

describe('Skip analytics exposes independent event quality', () => {
  beforeEach(() => {
    cy.stubApi()
  })

  it('surfaces malformed TradeEvent payloads without using ledger quality', () => {
    cy.intercept('GET', '/api/skip-analytics/summary*', {
      body: {
        days: 30,
        sample_size: 2,
        by_category: [
          { category: 'UNKNOWN', count: 2, share: 1 },
        ],
        by_symbol: [],
        by_side: {},
        top_reasons: [],
        daily: [],
        event_quality: {
          status: 'DEGRADED',
          total_event_count: 2,
          valid_event_count: 0,
          invalid_event_count: 2,
          issues: [
            { code: 'PAYLOAD_NOT_OBJECT', count: 2 },
          ],
        },
      },
    }).as('skipEventEvidence')

    cy.visit('/#/skip-analytics')
    cy.contains('button', '分析').click()
    cy.wait('@skipEventEvidence')
    cy.get('[data-testid="event-quality-alert"]')
      .should('have.attr', 'data-quality-status', 'DEGRADED')
      .and('contain', '2 条事件 payload')
  })
})
