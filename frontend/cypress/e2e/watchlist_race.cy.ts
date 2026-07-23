describe('Watchlist latest-response wins', () => {
  it('keeps new quant and AI results when the initial score request finishes late', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/watchlist/scores', {
      delay: 700,
      body: {
        scores: [
          {
            id: 90,
            symbol: 'NVDA.US',
            market: 'US',
            score: 12,
            rationale: '旧量化结果',
            confidence: 0.2,
            recommended_action: 'AVOID',
            source: 'quant_v1',
            created_at: '2026-07-24T00:00:00Z',
            expires_at: '2026-07-24T06:00:00Z',
            is_stale: false,
          },
        ],
        reviews: [
          {
            id: 91,
            symbol: 'NVDA.US',
            market: 'US',
            score: 20,
            rationale: '旧 AI 复核',
            confidence: 0.2,
            recommended_action: 'HOLD',
            source: 'llm',
            created_at: '2026-07-24T00:00:00Z',
            expires_at: '2026-07-24T01:00:00Z',
            is_stale: false,
          },
        ],
      },
    }).as('getDelayedScores')

    cy.visit('/#/watchlist')
    cy.wait('@getWatchlist')
    cy.get('[data-testid="watchlist-quant-rank"]').click()
    cy.get('button[aria-label="对 NVDA.US 进行 AI 复核"]').click()
    cy.wait('@quantRankWatchlist')
    cy.wait('@scoreWatchlistSymbol')
    cy.wait('@getDelayedScores')

    cy.get('[data-testid="watchlist-table"] tbody tr')
      .contains('tr', 'NVDA.US')
      .within(() => {
        cy.get('[data-testid="watchlist-quant-score"]')
          .should('contain', '56')
          .and('contain', '优选')
          .and('not.contain', '12')
        cy.get('[data-testid="watchlist-review-score"]')
          .should('contain', '88')
          .and('contain', 'AI 复核')
          .and('not.contain', '20')
      })
  })

  it('does not let a slow latest-run request replace a manual refresh', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/universe/latest', {
      delay: 700,
      body: {
        id: 20,
        as_of_date: '2026-07-22',
        algorithm_version: 'old-selector',
        source_version: 'old-catalog',
        status: 'COMPLETE',
        candidate_count: 1,
        evaluable_count: 1,
        selected_count: 0,
        coverage_ratio: 1,
        parameters: {},
        error: '',
        started_at: '2026-07-23T01:00:00Z',
        completed_at: '2026-07-23T01:00:01Z',
        created_at: '2026-07-23T01:00:00Z',
        items: [],
      },
    }).as('getDelayedUniverse')
    cy.intercept('POST', '/api/universe/refresh', {
      body: {
        run: {
          id: 21,
          as_of_date: '2026-07-24',
          algorithm_version: 'index-liquidity-opportunity-v2',
          source_version: 'current-catalog',
          status: 'COMPLETE',
          candidate_count: 1,
          evaluable_count: 1,
          selected_count: 1,
          coverage_ratio: 1,
          parameters: {},
          error: '',
          started_at: '2026-07-24T02:00:00Z',
          completed_at: '2026-07-24T02:00:01Z',
          created_at: '2026-07-24T02:00:00Z',
          items: [
            {
              symbol: 'NVDA.US',
              market: 'US',
              alias: 'NVIDIA',
              sector: 'Semiconductors',
              memberships: ['NASDAQ_100', 'DJIA'],
              selected: true,
              shadow_enabled: true,
              is_trading_target: true,
              rank: 1,
              score: 90,
              metrics: {
                price: 180,
                avg_dollar_volume: 12000000000,
                relative_spread_bps: 1.5,
                realized_vol_20d: 0.4,
                atr_pct_14d: 2.2,
                momentum_5d_pct: 2,
                trend_efficiency_10d: 0.3,
                opportunity_to_cost_ratio: 8,
              },
              exclusion_reasons: [],
              created_at: '2026-07-24T02:00:01Z',
            },
          ],
        },
        added_symbols: [],
        removed_symbols: [],
        retained_symbols: ['NVDA.US'],
        shadow_enabled_symbols: [],
        shadow_disabled_symbols: [],
        shadow_failed_symbols: [],
        applied: false,
        reason: 'observation only',
      },
    }).as('refreshCurrentUniverse')

    cy.visit('/#/watchlist')
    cy.get('[data-testid="universe-refresh"]').click()
    cy.wait('@refreshCurrentUniverse')
    cy.wait('@getDelayedUniverse')
    cy.get('[data-testid="universe-as-of"]').should('contain', '2026-07-24')
    cy.get('[data-testid="universe-panel"]')
      .should('contain', 'Shadow 已启用')
      .and('not.contain', 'old-selector')
  })
})
