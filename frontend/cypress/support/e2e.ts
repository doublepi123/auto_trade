type EngineState = 'flat' | 'long' | 'short'

interface StatusStub {
  engine_state: EngineState
  paused: boolean
  kill_switch: boolean
  protective_exit_permitted: boolean
  runner_running: boolean
  daily_pnl: number
  consecutive_losses: number
  cumulative_realized_pnl: number
  peak_realized_pnl: number
  drawdown_amount: number
  max_drawdown_amount: number | null
  last_price: number
  last_trigger_price: number
  last_trigger_at: string | null
  last_action_message: string
  trading_session_mode: 'ANY' | 'RTH_ONLY'
  is_trading_hours: boolean
  execution_state: 'IDLE' | 'REDUCING'
  reduction_reason: string
  reduction_started_at: string | null
}

function initialStatus(): StatusStub {
  return {
    engine_state: 'flat',
    paused: false,
    kill_switch: false,
    protective_exit_permitted: false,
    runner_running: false,
    daily_pnl: 0,
    consecutive_losses: 0,
    cumulative_realized_pnl: 900,
    peak_realized_pnl: 1000,
    drawdown_amount: 100,
    max_drawdown_amount: 250,
    last_price: 0,
    last_trigger_price: 0,
    last_trigger_at: null,
    last_action_message: '',
    trading_session_mode: 'ANY',
    is_trading_hours: true,
    execution_state: 'IDLE',
    reduction_reason: '',
    reduction_started_at: null,
  }
}

Cypress.Commands.add('setupApp', () => {
  // No-op: API key auth removed
})

Cypress.Commands.add('stubApi', () => {
  let status = initialStatus()
  const completeStatisticsQuality = {
    status: 'COMPLETE',
    known_exclusion_count: 0,
    unresolved_issue_count: 0,
    omitted_day_count: 0,
    items: [],
  }
  let strategyShadowConfig = {
    enabled: true,
    symbol: 'NVDA.US',
    zscore_window_1m_bars: 30,
    zscore_window_5m_bars: 20,
    breach_zscore: -2,
    reclaim_zscore: -1,
    five_minute_zscore_max: -0.5,
    adx_period: 14,
    max_adx: 25,
    realized_vol_window_bars: 20,
    min_realized_vol: 0.001,
    max_realized_vol: 0.04,
    stop_loss_pct: 0.5,
    profit_target_pct: 0.6,
    max_holding_minutes: 60,
    entry_cutoff_minutes_before_close: 45,
    flatten_minutes_before_close: 15,
    arm_ttl_bars: 10,
    max_entries_per_day: 2,
    entry_cooldown_minutes: 15,
    slippage_bps: 2,
    estimated_fee_rate_us: 0.0005,
    estimated_fee_rate_hk: 0.003,
    algorithm_version: 'strategy-v2-rth-mr-v4-frozen-config',
    mode: 'SHADOW',
    order_submission_allowed: false,
    allow_position_addons: false,
    short_entries_enabled: false,
    config_version: 'shadow-stub-v1',
    updated_at: '2026-07-12T02:00:00Z',
  }
  const strategyShadowMetrics = {
    bars: 120,
    eligible_bars: 76,
    breaches: 8,
    reclaims: 5,
    entries: 5,
    exits: 4,
    closed_trades: 4,
    win_rate: 0.75,
    gross_pnl: 38.4,
    fees: 4.2,
    net_pnl: 34.2,
    max_drawdown: 7.8,
    avg_holding_minutes: 21.5,
    avg_mae_pct: 0.0032,
    avg_mfe_pct: 0.0078,
    comparison_available: false,
    live_action_count: null,
    action_agreement_rate: null,
    net_pnl_delta_vs_live: null,
  }
  let strategyShadowForwardValidation = {
    registration: {
      id: 1,
      symbol: 'NVDA.US',
      market: 'US',
      market_timezone: 'America/New_York',
      candidate_algorithm_version: 'strategy-v2-causal-trend-prewarm-v1',
      source_config_version: 'shadow-stub-v1',
      evaluator_digest: 'a1b2c3d4e5f6789012345678901234567890123456789012345678901234567890',
      registered_at: '2026-07-10T12:00:00Z',
      eligible_after: '2026-07-10T13:30:00Z',
      minimum_ready_pairs: 5,
      minimum_mature_pairs: 20,
    },
    status: 'COLLECTING',
    mode: 'SHADOW',
    order_submission_allowed: false,
    automatic_promotion_allowed: false,
    historical_target_backfill_allowed: false,
    evaluation_scope: 'FORWARD_OUT_OF_SAMPLE',
    included_pairs: 2,
    excluded_targets: 1,
    remaining_ready_pairs: 3,
    remaining_mature_pairs: 18,
    blockers: [] as string[],
    baseline_metrics: {
      ...strategyShadowMetrics,
      bars: 780,
      eligible_bars: 49,
      entries: 3,
      exits: 3,
      closed_trades: 3,
      net_pnl: 6.2,
      max_drawdown: 8.4,
    },
    candidate_metrics: {
      ...strategyShadowMetrics,
      bars: 780,
      eligible_bars: 78,
      entries: 4,
      exits: 4,
      closed_trades: 4,
      net_pnl: 18.7,
      max_drawdown: 6.1,
    },
    daily: [
      {
        target_session_date: '2026-07-10',
        seed_session_date: '2026-07-09',
        target_open_at: '2026-07-10T13:30:00Z',
        evaluated_at: '2026-07-10T20:15:00Z',
        disposition: 'INCLUDED',
        exclusion_reason: '',
        structural_failure: false,
        target_bars: 390,
        target_bars_sha256: '1111111111111111111111111111111111111111111111111111111111111111',
        seed_bars_sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        baseline_input_sha256: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        candidate_input_sha256: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        same_target_bars: true,
        baseline_replay_match: true,
        session_local_invariant: true,
        baseline: {
          session_date: '2026-07-10',
          seed_session_date: '2026-07-09',
          trend_context_cutoff_at: '2026-07-09T20:00:00Z',
          overnight_gap_pct: 0.0125,
          first_ready_at: '2026-07-10T15:49:00Z',
          bars: 390,
          ready_bars: 251,
          warmup_lost_bars: 139,
          eligible_bars: 25,
          hourly_eligibility: [],
        },
        candidate: {
          session_date: '2026-07-10',
          seed_session_date: '2026-07-09',
          trend_context_cutoff_at: '2026-07-09T20:00:00Z',
          overnight_gap_pct: 0.0125,
          first_ready_at: '2026-07-10T14:34:00Z',
          bars: 390,
          ready_bars: 326,
          warmup_lost_bars: 64,
          eligible_bars: 40,
          hourly_eligibility: [],
        },
        baseline_metrics: {
          ...strategyShadowMetrics,
          bars: 390,
          eligible_bars: 25,
          entries: 1,
          exits: 1,
          closed_trades: 1,
          net_pnl: -4.5,
          max_drawdown: 5.2,
        },
        candidate_metrics: {
          ...strategyShadowMetrics,
          bars: 390,
          eligible_bars: 40,
          entries: 2,
          exits: 2,
          closed_trades: 2,
          net_pnl: 8,
          max_drawdown: 2.9,
        },
        baseline_result_sha256: 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        candidate_result_sha256: 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
        evidence_digest_sha256: 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
      },
      {
        target_session_date: '2026-07-13',
        seed_session_date: '2026-07-10',
        target_open_at: '2026-07-13T13:30:00Z',
        evaluated_at: '2026-07-13T20:15:00Z',
        disposition: 'INCLUDED',
        exclusion_reason: '',
        structural_failure: false,
        target_bars: 390,
        target_bars_sha256: '2222222222222222222222222222222222222222222222222222222222222222',
        seed_bars_sha256: 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
        baseline_input_sha256: 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
        candidate_input_sha256: 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
        same_target_bars: true,
        baseline_replay_match: true,
        session_local_invariant: true,
        baseline: {
          session_date: '2026-07-13',
          seed_session_date: '2026-07-10',
          trend_context_cutoff_at: '2026-07-10T20:00:00Z',
          overnight_gap_pct: -0.006,
          first_ready_at: '2026-07-13T15:49:00Z',
          bars: 390,
          ready_bars: 251,
          warmup_lost_bars: 139,
          eligible_bars: 24,
          hourly_eligibility: [],
        },
        candidate: {
          session_date: '2026-07-13',
          seed_session_date: '2026-07-10',
          trend_context_cutoff_at: '2026-07-10T20:00:00Z',
          overnight_gap_pct: -0.006,
          first_ready_at: '2026-07-13T14:34:00Z',
          bars: 390,
          ready_bars: 326,
          warmup_lost_bars: 64,
          eligible_bars: 38,
          hourly_eligibility: [],
        },
        baseline_metrics: {
          ...strategyShadowMetrics,
          bars: 390,
          eligible_bars: 24,
          entries: 2,
          exits: 2,
          closed_trades: 2,
          net_pnl: 10.7,
          max_drawdown: 8.4,
        },
        candidate_metrics: {
          ...strategyShadowMetrics,
          bars: 390,
          eligible_bars: 38,
          entries: 2,
          exits: 2,
          closed_trades: 2,
          net_pnl: 10.7,
          max_drawdown: 6.1,
        },
        baseline_result_sha256: 'abababababababababababababababababababababababababababababababab',
        candidate_result_sha256: 'cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd',
        evidence_digest_sha256: 'efefefefefefefefefefefefefefefefefefefefefefefefefefefefefef',
      },
      {
        target_session_date: '2026-07-14',
        seed_session_date: '2026-07-13',
        target_open_at: '2026-07-14T13:30:00Z',
        evaluated_at: '2026-07-14T20:15:00Z',
        disposition: 'EXCLUDED',
        exclusion_reason: 'TARGET_SESSION_INCOMPLETE',
        structural_failure: false,
        target_bars: 210,
        target_bars_sha256: '',
        seed_bars_sha256: '',
        baseline_input_sha256: '',
        candidate_input_sha256: '',
        same_target_bars: false,
        baseline_replay_match: null,
        session_local_invariant: null,
        baseline: null,
        candidate: null,
        baseline_metrics: null,
        candidate_metrics: null,
        baseline_result_sha256: '',
        candidate_result_sha256: '',
        evidence_digest_sha256: '1212121212121212121212121212121212121212121212121212121212121212',
      },
    ],
  }

  cy.intercept('GET', '/api/strategy', {
    body: {
      id: 1, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
      short_selling: false, max_daily_loss: 5000, max_drawdown_amount: 250, max_consecutive_losses: 3,
      min_profit_amount: 0,
      auto_resume_minutes: 3,
      llm_interval_minutes: 2,
      fee_rate_us: 0.0005,
      fee_rate_hk: 0.003,
      min_repricing_pct: 0.003,
      llm_action_cooldown_seconds: 60,
      trading_session_mode: 'ANY',
      allow_position_addons: false,
      max_position_quantity: 100,
      max_position_notional: 5000,
      max_risk_per_trade: 250,
      stop_loss_pct: 1,
      max_holding_minutes: 60,
      entry_cutoff_minutes_before_close: 45,
      flatten_minutes_before_close: 15,
      llm_order_execution_enabled: false,
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
          symbol: 'NVDA.US',
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
          symbol: 'NVDA.US',
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

  cy.intercept('GET', '/api/positions/pnl', {
    body: {
      positions: [],
      total_unrealized_pnl: 0,
      total_cost_basis: 0,
      total_unrealized_pnl_pct: null,
      available: true,
      error: null,
    },
  }).as('getPositionPnl')

  cy.intercept('GET', '/api/metrics/summary*', {
    body: {
      trade_count: 8,
      win_rate: 62.5,
      total_pnl: 250,
      max_drawdown: 60,
      max_drawdown_amount: 60,
      profit_factor: 1.8,
      sharpe_ratio: 1.2,
      avg_pnl: 31.25,
      window_days: 30,
      currency: 'USD',
      totals_comparable: true,
      by_currency: [
        {
          currency: 'USD',
          trade_count: 8,
          win_rate: 62.5,
          total_pnl: 250,
          max_drawdown: 24,
          max_drawdown_amount: 60,
          profit_factor: 1.8,
          sharpe_ratio: 1.2,
          avg_pnl: 31.25,
        },
      ],
      statistics_quality: completeStatisticsQuality,
    },
  }).as('getMetricsSummary')

  cy.intercept('GET', '/api/equity/curve*', {
    body: {
      points: [
        { date: '2026-06-10', realized_pnl: 0, cumulative_pnl: 0, drawdown: 0, trade_count: 0 },
        { date: '2026-06-11', realized_pnl: 120, cumulative_pnl: 120, drawdown: 0, trade_count: 2 },
        { date: '2026-06-12', realized_pnl: -60, cumulative_pnl: 60, drawdown: 60, trade_count: 1 },
        { date: '2026-06-13', realized_pnl: 200, cumulative_pnl: 260, drawdown: 0, trade_count: 3 },
      ],
      total_realized_pnl: 260,
      max_drawdown: 60,
      statistics_quality: completeStatisticsQuality,
    },
  }).as('getEquityCurve')

  cy.intercept('GET', '/api/pnl/by-symbol*', {
    body: {
      rows: [
        { symbol: 'AAPL.US', realized_pnl: 300, trade_count: 6, win_count: 4, win_rate: 66.7, contribution_share: 0.75, largest_win: 120, largest_loss: -40 },
        { symbol: 'NVDA.US', realized_pnl: -50, trade_count: 2, win_count: 0, win_rate: 0, contribution_share: -0.125, largest_win: 0, largest_loss: -50 },
      ],
      total_realized_pnl: 250,
      statistics_quality: completeStatisticsQuality,
    },
  }).as('getPnlBySymbol')

  cy.intercept('GET', '/api/risk/history*', {
    body: {
      points: [
        { created_at: '2026-06-16T10:00:00Z', engine_state: 'flat', paused: false, kill_switch: false, daily_pnl: -100, consecutive_losses: 1 },
        { created_at: '2026-06-16T10:05:00Z', engine_state: 'flat', paused: false, kill_switch: false, daily_pnl: 50, consecutive_losses: 0 },
        { created_at: '2026-06-16T10:10:00Z', engine_state: 'flat', paused: true, kill_switch: false, daily_pnl: -200, consecutive_losses: 2 },
      ],
      latest: { created_at: '2026-06-16T10:10:00Z', engine_state: 'flat', paused: true, kill_switch: false, daily_pnl: -200, consecutive_losses: 2 },
    },
  }).as('getRiskHistory')

  cy.intercept('GET', '/api/broker/candles*', {
    body: {
      symbol: 'AAPL.US',
      period: 'DAY',
      count: 2,
      bars: [
        { timestamp: '2026-06-14T13:30:00Z', open: 100, high: 110, low: 95, close: 105, volume: 1000 },
        { timestamp: '2026-06-15T13:30:00Z', open: 105, high: 120, low: 100, close: 115, volume: 1200 },
      ],
      csv_text: 'timestamp,open,high,low,close,volume\n2026-06-14T13:30:00Z,100,110,95,105,1000\n2026-06-15T13:30:00Z,105,120,100,115,1200',
    },
  }).as('getBrokerCandles')

  cy.intercept('GET', '/api/llm-interactions/*', {
    body: {
      id: 1,
      interaction_type: 'analyze',
      symbol: 'AAPL.US',
      market: 'US',
      prompt: 'suggest interval',
      raw_response: '{"buy_low": 90, "sell_high": 190}',
      parsed_response: { buy_low: 90, sell_high: 190 },
      context_snapshot: { price: 120 },
      success: true,
      error: '',
      order_action: 'BUY',
      order_status: null,
      order_id: null,
      applied: true,
      prompt_variant: null,
      created_at: '2026-06-16T12:00:00Z',
    },
  }).as('getLLMInteraction')

  cy.intercept('GET', '/api/calendar/session*', {
    body: {
      market: 'US',
      symbol: 'AAPL.US',
      status: 'rth',
      is_trading: true,
      local_time: '2026-06-16 10:30:00 EDT',
      utc_time: '2026-06-16T14:30:00Z',
      next_open: '2026-06-17T13:30:00Z',
    },
  }).as('getMarketSession')

  cy.intercept('GET', '/api/notifications?*', (req) => {
    let items = [
      { id: 1, title: '风控熔断', content: 'kill switch triggered', severity: 'CRITICAL', success: true, error: '', created_at: '2026-06-16T12:00:00Z' },
      { id: 2, title: '日报', content: 'AAPL.US +200', severity: 'INFO', success: true, error: '', created_at: '2026-06-16T11:00:00Z' },
      { id: 3, title: '发送失败', content: 'webhook timeout', severity: 'WARNING', success: false, error: 'connection refused', created_at: '2026-06-15T10:00:00Z' },
    ]
    const params = req.query
    if (params.severity) {
      items = items.filter((i) => i.severity === params.severity)
    }
    if (params.success !== undefined) {
      items = items.filter((i) => String(i.success) === params.success)
    }
    if (params.q) {
      const q = String(params.q).toLowerCase()
      items = items.filter((i) =>
        i.title.toLowerCase().includes(q) ||
        i.content.toLowerCase().includes(q) ||
        i.error.toLowerCase().includes(q)
      )
    }
    if (params.from_date) {
      items = items.filter((i) => i.created_at >= String(params.from_date))
    }
    if (params.to_date) {
      const end = String(params.to_date) + 'T23:59:59Z'
      items = items.filter((i) => i.created_at <= end)
    }
    req.reply({
      body: { items, total: items.length, page: 1, page_size: 50 },
    })
  }).as('getNotifications')

  cy.intercept('GET', '/api/notifications/export*', (req) => {
    const format = req.query.format === 'json' ? 'json' : 'csv'
    const rows = [
      { id: 1, created_at: '2026-06-16T12:00:00Z', severity: 'CRITICAL', success: true, title: '风控熔断', content: 'kill switch triggered', error: '' },
      { id: 2, created_at: '2026-06-16T11:00:00Z', severity: 'INFO', success: true, title: '日报', content: 'AAPL.US +200', error: '' },
      { id: 3, created_at: '2026-06-15T10:00:00Z', severity: 'WARNING', success: false, title: '发送失败', content: 'webhook timeout', error: 'connection refused' },
    ]
    if (format === 'json') {
      req.reply({ body: rows })
    } else {
      const lines = rows.map((r) =>
        `${r.id},${r.created_at},${r.severity},${r.success},${r.title},${r.content},${r.error}`
      )
      req.reply({
        body: ['id,created_at,severity,success,title,content,error', ...lines].join('\n'),
        headers: { 'content-type': 'text/csv' },
      })
    }
  }).as('exportNotifications')

  cy.intercept('POST', '/api/reports/schedule/run', {
    body: { sent: true, symbol: 'AAPL.US', title: '交易日报 · AAPL.US', error: null },
  }).as('runScheduledReport')

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
      items: [
        {
          id: 1,
          broker_order_id: 'order-1',
          symbol: 'AAPL.US',
          market: 'US',
          source: 'broker',
          side: 'BUY',
          quantity: 10,
          executed_quantity: 10,
          price: 150,
          executed_price: 149.5,
          status: 'FILLED',
          created_at: '2026-06-16T10:00:00Z',
          filled_at: '2026-06-16T10:01:00Z',
          cancellable: false,
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
      scope: 'today',
    },
  }).as('getOrders')

  cy.intercept('GET', '/api/trade-notes', {
    body: {
      items: [
        { order_id: 1, note: 'scalp', tags: ['momentum'], rating: 4, created_at: '2026-06-16T10:00:00Z', updated_at: '2026-06-16T10:00:00Z' },
      ],
      total: 1,
      page: 1,
      page_size: 50,
    },
  }).as('getTradeNotes')

  cy.intercept('GET', '/api/trade-notes/analytics', {
    body: {
      total: 2,
      rated_count: 2,
      avg_rating: 4.0,
      rating_distribution: { 1: 0, 2: 0, 3: 1, 4: 0, 5: 1 },
      top_tags: [{ tag: 'good', count: 2 }],
      distinct_symbols: 1,
    },
  }).as('getTradeNoteAnalytics')

  cy.intercept('GET', '/api/trades?*', {
    body: { items: [], total: 0, statistics_quality: completeStatisticsQuality },
  }).as('getClosedTrades')

  cy.intercept('GET', '/api/trades/stats*', {
    body: {
      total_trades: 0,
      win_count: 0,
      loss_count: 0,
      breakeven_count: 0,
      win_rate: 0,
      total_gross_pnl: 0,
      total_net_pnl: 0,
      avg_win: null,
      avg_loss: null,
      expectancy: 0,
      profit_factor: null,
      payoff_ratio: null,
      largest_win: null,
      largest_loss: null,
      current_streak_type: 'none',
      current_streak_count: 0,
      max_win_streak: 0,
      max_loss_streak: 0,
      avg_hold_seconds: null,
      total_fees: 0,
      actual_fee_coverage_pct: 0,
      avg_slippage_bps: null,
      avg_ack_latency_ms: null,
      statistics_quality: completeStatisticsQuality,
    },
  }).as('getTradeStats')

  cy.intercept('GET', '/api/trades/analytics/calendar*', {
    body: { items: [], total_trades: 0, total_net_pnl: 0, statistics_quality: completeStatisticsQuality },
  }).as('getTradeCalendar')

  cy.intercept('GET', '/api/trades/analytics/hold-duration*', {
    body: { items: [], total_trades: 0, statistics_quality: completeStatisticsQuality },
  }).as('getTradeHoldDuration')

  cy.intercept('GET', '/api/trades/analytics/pnl-distribution*', {
    body: { items: [], total_trades: 0, total_net_pnl: 0, statistics_quality: completeStatisticsQuality },
  }).as('getTradePnlDistribution')

  cy.intercept('GET', '/api/trades/analytics/monthly*', {
    body: { items: [], total_trades: 0, total_net_pnl: 0, statistics_quality: completeStatisticsQuality },
  }).as('getTradeMonthlySummary')

  cy.intercept('GET', '/api/trades/analytics/weekday*', {
    body: { items: [], total_trades: 0, total_net_pnl: 0, statistics_quality: completeStatisticsQuality },
  }).as('getTradeWeekdayAttribution')

  cy.intercept('GET', '/api/events*', (req) => {
    const items = [
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
    ]
    const params = req.query
    let filtered = items
    if (params.event_type) {
      const types = Array.isArray(params.event_type) ? params.event_type : [params.event_type]
      filtered = filtered.filter((i) => types.includes(i.event_type))
    }
    if (params.skip_category) {
      const cats = Array.isArray(params.skip_category) ? params.skip_category : [params.skip_category]
      filtered = filtered.filter((i) => cats.includes(i.payload?.skip_category))
    }
    req.reply({ body: { items: filtered, total: filtered.length, page: 1, page_size: 20 } })
  }).as('getEvents')

  cy.intercept('GET', '/api/reports/range*', {
    body: {
      period_type: 'range',
      symbol: 'AAPL.US',
      start_date: '2026-05-01',
      end_date: '2026-05-31',
      metrics: {
        total_pnl: 0,
        total_trades: 0,
        win_count: 0,
        loss_count: 0,
        win_rate: 0,
        profit_loss_ratio: 0,
        avg_pnl_per_trade: 0,
        max_profit: 0,
        max_loss: 0,
        max_drawdown: 0,
        llm_suggestions_count: 0,
        llm_applied_count: 0,
        llm_apply_rate: 0,
        llm_profitable_count: 0,
        llm_accuracy_rate: 0,
      },
      daily_points: [],
      attribution: [],
      details: [],
      statistics_quality: completeStatisticsQuality,
    },
  }).as('getReport')

  cy.intercept('GET', '/api/reports/export*', {
    body: 'date,symbol,pnl\n2026-05-01,AAPL.US,0\n',
    headers: {
      'content-type': 'text/csv',
    },
  }).as('exportReport')

  cy.intercept('GET', '/api/review?*', {
    body: {
      symbol: 'AAPL.US',
      from_date: '2026-05-01',
      to_date: '2026-05-31',
      days: [
        {
          date: '2026-05-19',
          symbol: 'AAPL.US',
          llm_interactions: [
            {
              id: 1,
              interaction_type: 'analyze',
              symbol: 'AAPL.US',
              market: 'US',
              success: true,
              order_action: 'BUY',
              order_status: null,
              order_id: null,
              applied: true,
              created_at: '2026-05-19T19:52:03.545862Z',
            },
          ],
          orders: [],
          events: [],
          snapshots: [],
          daily_pnl: 12.5,
          trade_count: 0,
          error_tags: [],
          included_in_statistics: true,
          statistics_quality: completeStatisticsQuality,
        },
      ],
      total_pnl: 12.5,
      total_trades: 0,
      all_error_tags: [],
      statistics_quality: completeStatisticsQuality,
    },
  }).as('getReview')

  cy.intercept('GET', '/api/review/export*', {
    body: 'date,symbol,pnl\n2026-05-19,AAPL.US,12.5\n',
    headers: {
      'content-type': 'text/csv',
    },
  }).as('exportReview')

  cy.intercept('GET', '/api/watchlist', {
    body: [
      {
        id: 1,
        symbol: 'NVDA.US',
        market: 'US',
        alias: 'Nvidia',
        source: 'manual',
        is_active: true,
        is_trading_target: true,
      },
      {
        id: 2,
        symbol: 'AAPL.US',
        market: 'US',
        alias: 'Apple',
        source: 'universe',
        is_active: true,
        is_trading_target: false,
      },
    ],
  }).as('getWatchlist')

  cy.intercept('GET', '/api/watchlist/quotes*', {
    body: [
      { symbol: 'NVDA.US', last_price: 180.5, bid: 180.4, ask: 180.6, timestamp: '2026-06-04T10:00:00Z' },
      { symbol: 'AAPL.US', last_price: 199.5, bid: 199.4, ask: 199.6, timestamp: '2026-06-04T10:00:00Z' },
    ],
  }).as('getWatchlistQuotes')

  cy.intercept('GET', '/api/watchlist/snapshots', {
    body: [
      {
        symbol: 'NVDA.US',
        market: 'US',
        alias: 'Nvidia',
        is_trading_target: true,
        last_price: 180.5,
        bid: 180.4,
        ask: 180.6,
        timestamp: '2026-06-04T10:00:00Z',
      },
      {
        symbol: 'AAPL.US',
        market: 'US',
        alias: 'Apple',
        is_trading_target: false,
        last_price: 199.5,
        bid: 199.4,
        ask: 199.6,
        timestamp: '2026-06-04T10:00:00Z',
      },
    ],
  }).as('getWatchlistSnapshots')

  cy.intercept('GET', '/api/watchlist/scores', {
    body: {
      scores: [
        {
          id: 1,
          symbol: 'NVDA.US',
          market: 'US',
          score: 56,
          rationale: '流动性充足，价差较窄，短周期波动可覆盖预估交易成本。',
          confidence: 0.82,
          recommended_action: 'CANDIDATE',
          source: 'quant_v3',
          created_at: '2026-06-18T10:00:00Z',
          expires_at: '2099-06-18T16:00:00Z',
          is_stale: false,
        },
      ],
      reviews: [
        {
          id: 5,
          symbol: 'NVDA.US',
          market: 'US',
          score: 82,
          rationale: '价格处于布林带中轨上方，成交量放大，短期动能偏强。',
          confidence: 0.85,
          recommended_action: 'BUY',
          source: 'llm',
          created_at: '2026-06-18T10:02:00Z',
          expires_at: '2099-06-18T11:02:00Z',
          is_stale: false,
        },
        {
          id: 2,
          symbol: 'AAPL.US',
          market: 'US',
          score: 50,
          rationale: 'LLM 不可用，返回中性兜底结果。',
          confidence: 0.5,
          recommended_action: 'HOLD',
          source: 'fallback_unconfigured',
          created_at: '2026-06-18T10:00:00Z',
          expires_at: '2026-06-18T11:00:00Z',
          is_stale: true,
        },
      ],
    },
  }).as('getWatchlistScores')

  cy.intercept('POST', '/api/watchlist/quant-rank*', {
    body: {
      scores: [
        {
          id: 3,
          symbol: 'NVDA.US',
          market: 'US',
          score: 56,
          rationale: '流动性充足，价差较窄，短周期波动可覆盖预估交易成本。',
          confidence: 0.82,
          recommended_action: 'CANDIDATE',
          source: 'quant_v3',
          created_at: '2026-07-24T02:10:00Z',
          expires_at: '2099-07-24T08:10:00Z',
          is_stale: false,
        },
        {
          id: 4,
          symbol: 'AAPL.US',
          market: 'US',
          score: 43,
          rationale: '流动性合格，但短周期机会成本比一般，继续观察。',
          confidence: 0.68,
          recommended_action: 'WATCH',
          source: 'quant_v3',
          created_at: '2026-07-24T02:10:00Z',
          expires_at: '2099-07-24T08:10:00Z',
          is_stale: false,
        },
      ],
    },
  }).as('quantRankWatchlist')

  cy.intercept('GET', '/api/universe/catalog', {
    body: [
      {
        symbol: 'NVDA.US',
        market: 'US',
        alias: 'NVIDIA',
        sector: 'Semiconductors',
        memberships: ['NASDAQ_100', 'DJIA'],
      },
      {
        symbol: 'AAPL.US',
        market: 'US',
        alias: 'Apple',
        sector: 'Technology Hardware',
        memberships: ['NASDAQ_100', 'DJIA'],
      },
      {
        symbol: 'JPM.US',
        market: 'US',
        alias: 'JPMorgan Chase',
        sector: 'Financials',
        memberships: ['DJIA'],
      },
    ],
  }).as('getUniverseCatalog')

  cy.intercept('GET', '/api/universe/latest', {
    body: {
      id: 7,
      as_of_date: '2026-07-23',
      algorithm_version: 'index-liquidity-opportunity-v2',
      source_version: 'nasdaq-100_djia-v1',
      status: 'COMPLETE',
      candidate_count: 3,
      evaluable_count: 3,
      selected_count: 2,
      coverage_ratio: 0.95,
      parameters: { max_selected: 8 },
      error: '',
      started_at: '2026-07-24T01:00:00Z',
      completed_at: '2026-07-24T01:00:10Z',
      created_at: '2026-07-24T01:00:00Z',
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
          score: 92.4,
          metrics: {
            price: 181.2,
            avg_dollar_volume: 12400000000,
            relative_spread_bps: 1.8,
            realized_vol_20d: 0.42,
            atr_pct_14d: 2.35,
            momentum_5d_pct: 3.1,
            trend_efficiency_10d: 0.31,
            opportunity_to_cost_ratio: 8.2,
          },
          exclusion_reasons: [],
          created_at: '2026-07-24T01:00:10Z',
        },
        {
          symbol: 'JPM.US',
          market: 'US',
          alias: 'JPMorgan Chase',
          sector: 'Financials',
          memberships: ['DJIA'],
          selected: true,
          shadow_enabled: false,
          is_trading_target: false,
          rank: 2,
          score: 78.6,
          metrics: {
            price: 291.5,
            avg_dollar_volume: 2100000000,
            relative_spread_bps: 2.3,
            realized_vol_20d: 0.25,
            atr_pct_14d: 1.48,
            momentum_5d_pct: 1.4,
            trend_efficiency_10d: 0.22,
            opportunity_to_cost_ratio: 5.9,
          },
          exclusion_reasons: [],
          created_at: '2026-07-24T01:00:10Z',
        },
        {
          symbol: 'AAPL.US',
          market: 'US',
          alias: 'Apple',
          sector: 'Technology Hardware',
          memberships: ['NASDAQ_100', 'DJIA'],
          selected: false,
          shadow_enabled: false,
          is_trading_target: false,
          rank: null,
          score: 66.1,
          metrics: {
            price: 214.8,
            avg_dollar_volume: 6800000000,
            relative_spread_bps: 2.0,
            realized_vol_20d: 0.3,
            atr_pct_14d: 1.72,
            momentum_5d_pct: 0.5,
            trend_efficiency_10d: 0.46,
            opportunity_to_cost_ratio: 6.1,
          },
          exclusion_reasons: ['SECTOR_CAP'],
          created_at: '2026-07-24T01:00:10Z',
        },
      ],
    },
  }).as('getUniverseLatest')

  cy.intercept('GET', '/api/universe/promotion-readiness', {
    body: {
      universe_run_id: 7,
      as_of_date: '2026-07-23',
      generated_at: '2026-07-24T01:05:00Z',
      priority_algorithm_version: 'selection-quant-gated-v2',
      items: [
        {
          symbol: 'NVDA.US',
          rank: 1,
          selection_score: 92.4,
          priority_rank: 1,
          priority_score: 92.4,
          quant_weight: 0,
          quant_adjustment: 0,
          quant_score: 24,
          quant_confidence: 0.84,
          quant_recommended_action: 'AVOID',
          quant_source: 'quant_v3',
          quant_fresh: false,
          quant_expires_at: '2026-07-23T20:00:00Z',
          is_trading_target: true,
          shadow_enabled: true,
          forward_status: 'COLLECTING',
          included_pairs: 2,
          minimum_ready_pairs: 5,
          minimum_mature_pairs: 20,
          remaining_ready_pairs: 3,
          remaining_mature_pairs: 18,
          blockers: [],
          baseline_metrics: {
            ...strategyShadowMetrics,
            closed_trades: 3,
            net_pnl: 6.2,
          },
          candidate_metrics: {
            ...strategyShadowMetrics,
            closed_trades: 4,
            net_pnl: 18.7,
          },
          review_ready: false,
          mature_evidence: false,
          automatic_promotion_allowed: false,
        },
        {
          symbol: 'JPM.US',
          rank: 2,
          selection_score: 78.6,
          priority_rank: 2,
          priority_score: 53.6,
          quant_weight: 0,
          quant_adjustment: -25,
          quant_score: 0,
          quant_confidence: 0,
          quant_recommended_action: 'AVOID',
          quant_source: 'quant_error_v3',
          quant_fresh: true,
          quant_expires_at: '2026-07-24T08:00:00Z',
          is_trading_target: false,
          shadow_enabled: true,
          forward_status: 'BLOCKED',
          included_pairs: 5,
          minimum_ready_pairs: 5,
          minimum_mature_pairs: 20,
          remaining_ready_pairs: 0,
          remaining_mature_pairs: 15,
          blockers: ['TARGET_INPUT_HASH_MISMATCH'],
          baseline_metrics: {
            ...strategyShadowMetrics,
            closed_trades: 2,
            net_pnl: -4.5,
          },
          candidate_metrics: {
            ...strategyShadowMetrics,
            closed_trades: 3,
            net_pnl: 2.4,
          },
          review_ready: false,
          mature_evidence: false,
          automatic_promotion_allowed: false,
        },
      ],
    },
  }).as('getUniversePromotionReadiness')

  cy.intercept('POST', '/api/watchlist/score', {
    body: {
      id: 3,
      symbol: 'NVDA.US',
      market: 'US',
      score: 88,
      rationale: '实时评分：突破近期高点，量价配合。',
      confidence: 0.9,
      recommended_action: 'BUY',
      source: 'llm',
      created_at: '2026-06-18T10:05:00Z',
      expires_at: '2099-06-18T11:05:00Z',
      is_stale: false,
    },
  }).as('scoreWatchlistSymbol')

  cy.intercept('GET', '/api/diagnostics', {
    body: {
      runner_running: false,
      thread_alive: false,
      quotes_subscribed: true,
      trigger_in_flight: false,
      pending_order_symbols: ['AAPL.US'],
      live_safety: {
        full_buying_power_usage_enabled: false,
        buying_power_usage_pct: 90,
        short_entries_enabled: false,
        allow_position_addons: false,
        max_position_quantity: 100,
        max_position_notional: 5000,
        max_risk_per_trade: 250,
        stop_loss_pct: 1,
        max_holding_minutes: 60,
        entry_cutoff_minutes_before_close: 45,
        flatten_minutes_before_close: 15,
        llm_shadow_mode: true,
        llm_order_execution_enabled: false,
        live_regime_gate_enabled: true,
        live_regime_max_data_age_seconds: 900,
        live_max_entries_per_symbol_per_day: 2,
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
        protective_exit_permitted: false,
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

  cy.intercept('GET', '/api/strategy/llm-interval/status', {
    body: {
      enabled: true,
      shadow_mode: false,
      policy_status: 'LIVE',
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
      last_applied_values: { buy_low: 220.42, sell_high: 221.42 },
      reject_reason: null,
      budget: {
        max_symbols_per_cycle: 5,
        max_analyses_per_hour: 60,
        tracked_symbol_count: 2,
        effective_symbol_budget: 2,
        used_analyses_last_hour: 12,
        remaining_analyses_this_hour: 48,
      },
      symbol_statuses: [
        {
          symbol: 'AAPL.US',
          market: 'US',
          is_primary: true,
          has_pending_order: false,
          buy_cooldown_remaining_seconds: 0,
          sell_cooldown_remaining_seconds: 45,
          last_analysis_at: '2026-05-19T19:52:03.545862Z',
          next_analysis_at: '2026-05-19T19:53:03.545862Z',
          last_status: 'COOLDOWN',
          last_skip_reason: '同方向冷却中',
        },
        {
          symbol: 'NVDA.US',
          market: 'US',
          is_primary: false,
          has_pending_order: true,
          buy_cooldown_remaining_seconds: 120,
          sell_cooldown_remaining_seconds: 0,
          last_analysis_at: '2026-05-19T19:51:03.545862Z',
          next_analysis_at: '2026-05-19T19:54:03.545862Z',
          last_status: 'PENDING_ORDER',
          last_skip_reason: null,
        },
      ],
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

  cy.intercept('GET', '/api/llm-usage/summary*', {
    body: {
      days: 30,
      total_interactions: 0,
      successful_interactions: 0,
      total_prompt_tokens: 0,
      total_completion_tokens: 0,
      total_tokens: 0,
      by_day: [],
      by_type: [],
    },
  }).as('getLLMUsageSummary')

  cy.intercept('POST', '/api/backtest/run', {
    body: {
      params: {
        symbol: 'AAPL.US',
        buy_low: 100,
        sell_high: 200,
        short_selling: false,
        min_profit_amount: 0,
        max_daily_loss: 5000,
        max_drawdown_amount: 0,
        max_consecutive_losses: 3,
        quantity: 2,
        initial_cash: 10000,
        fee_rate: 0,
        fixed_fee: 0,
        slippage_pct: 0,
        stop_loss_pct: 0,
        trailing_stop_pct: 0,
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
      skipped_signals: [
        {
          timestamp: '2026-05-22T10:02:00Z',
          action: 'SELL',
          price: 101,
          reason: 'net profit below min_profit_amount',
          state: 'long',
          category: 'FEE',
        },
        {
          timestamp: '2026-05-22T10:03:00Z',
          action: 'BUY',
          price: 100,
          reason: 'maximum drawdown amount reached',
          state: 'flat',
          category: 'DRAWDOWN',
        },
      ],
      fee_sensitivity: [
        { fee_rate: 0, total_pnl: 200, total_return_pct: 2, max_drawdown_pct: 0 },
        { fee_rate: 0.001, total_pnl: 199.4, total_return_pct: 1.994, max_drawdown_pct: 0 },
      ],
    },
  }).as('runBacktest')

  cy.intercept('POST', '/api/backtest/sweep', {
    body: {
      rows: [
        {
          rank: 1,
          params: { buy_low: 100, sell_high: 210, min_profit_amount: 0 },
          metrics: {
            total_pnl: 220, total_return_pct: 2.2, max_drawdown_pct: 0, win_rate: 100,
            sharpe_ratio: 1.8, sortino_ratio: 1.9, calmar_ratio: 6.0,
            profit_factor: null, profit_loss_ratio: null,
          },
        },
        {
          rank: 2,
          params: { buy_low: 100, sell_high: 200, min_profit_amount: 0 },
          metrics: {
            total_pnl: 200, total_return_pct: 2, max_drawdown_pct: 0.2, win_rate: 100,
            sharpe_ratio: 1.55, sortino_ratio: 1.6, calmar_ratio: 5.0,
            profit_factor: null, profit_loss_ratio: null,
          },
        },
        {
          rank: 3,
          params: { buy_low: 110, sell_high: 210, min_profit_amount: 0 },
          metrics: {
            total_pnl: 160, total_return_pct: 1.6, max_drawdown_pct: 0.8, win_rate: 100,
            sharpe_ratio: 1.3, sortino_ratio: 1.4, calmar_ratio: 2.8,
            profit_factor: null, profit_loss_ratio: null,
          },
        },
        {
          rank: 4,
          params: { buy_low: 110, sell_high: 200, min_profit_amount: 0 },
          metrics: {
            total_pnl: 90, total_return_pct: 0.9, max_drawdown_pct: 1.3, win_rate: 100,
            sharpe_ratio: 0.9, sortino_ratio: 1.0, calmar_ratio: 1.2,
            profit_factor: null, profit_loss_ratio: null,
          },
        },
      ],
      best: {
        rank: 1,
        params: { buy_low: 100, sell_high: 210, min_profit_amount: 0 },
        metrics: {
          total_pnl: 220, total_return_pct: 2.2, max_drawdown_pct: 0, win_rate: 100,
          sharpe_ratio: 1.8, sortino_ratio: 1.9, calmar_ratio: 6.0,
          profit_factor: null, profit_loss_ratio: null,
        },
      },
      heatmap: {
        x_axis: 'sell_high',
        y_axis: 'buy_low',
        z_metric: 'sharpe_ratio',
        cells: [
          { buy_low: 100, sell_high: 200, value: 1.55 },
          { buy_low: 100, sell_high: 210, value: 1.8 },
          { buy_low: 110, sell_high: 200, value: 0.9 },
          { buy_low: 110, sell_high: 210, value: 1.3 },
        ],
      },
      evaluated_count: 4,
      skipped_count: 0,
      sort_by: 'sharpe_ratio',
    },
  }).as('runBacktestSweep')

  cy.intercept('POST', '/api/backtest/walk-forward', {
    body: {
      windows: [
        {
          index: 0,
          start: '2026-05-22T10:00:00Z',
          end: '2026-05-22T10:09:00Z',
          train_size: 6,
          test_size: 4,
          best_params: { buy_low: 95, sell_high: 200, min_profit_amount: 0 },
          test_metrics: {
            initial_cash: 100000, final_equity: 102100, total_pnl: 2100,
            total_return_pct: 2.1, max_drawdown_pct: 0.5, trade_count: 4,
            closed_trade_count: 2, winning_trades: 2, losing_trades: 0, win_rate: 100,
            avg_holding_minutes: 2, fees_paid: 0, skipped_signals: 0, final_state: 'flat',
            sharpe_ratio: 1.2, sortino_ratio: 1.3, calmar_ratio: 4.2,
            profit_factor: null, profit_loss_ratio: null,
          },
        },
      ],
      summary: {
        window_count: 1, evaluated_window_count: 1,
        mean_test_return_pct: 2.1, median_test_return_pct: 2.1, mean_test_metric: 1.2,
        profitable_window_pct: 100, test_return_std_pct: 0,
      },
      sort_by: 'sharpe_ratio', train_size: 6, test_size: 4, step: 4,
    },
  }).as('runWalkForward')

  cy.intercept('POST', '/api/backtest/stress', {
    body: {
      scenarios_run: 20,
      baseline_return_pct: 2.0,
      median_return_pct: 1.8,
      p5_return_pct: -1.2,
      p95_return_pct: 4.5,
      worst_return_pct: -3.0,
      worst_drawdown_pct: 5.0,
      profitable_scenario_pct: 75,
      jitter_pct: 2.0,
      seed: 7,
      returns: [-3.0, -1.2, 1.8, 2.0, 4.5],
    },
  }).as('runStressTest')

  cy.intercept('GET', '/api/backtest/runs', {
    body: { items: [], total: 0, page: 1, page_size: 50 },
  }).as('listBacktestRuns')

  cy.intercept('POST', '/api/backtest/runs', (req) => {
    req.reply({
      body: {
        id: 1,
        name: req.body.name,
        symbol: 'AAPL.US',
        params: req.body.params,
        metrics: req.body.metrics,
        created_at: '2026-06-16T12:00:00Z',
      },
    })
  }).as('saveBacktestRun')

  cy.intercept('GET', '/api/backtest/runs/compare*', {
    body: {
      runs: [
        { id: 1, name: 'A', symbol: 'AAPL.US', params: { buy_low: 100, sell_high: 200 }, metrics: { total_pnl: 100, total_return_pct: 1, max_drawdown_pct: 0.5, trade_count: 2, win_rate: 100, sharpe_ratio: 1.2 } },
        { id: 2, name: 'B', symbol: 'AAPL.US', params: { buy_low: 110, sell_high: 200 }, metrics: { total_pnl: 80, total_return_pct: 0.8, max_drawdown_pct: 0.7, trade_count: 2, win_rate: 100, sharpe_ratio: 1.0 } },
      ],
    },
  }).as('compareBacktestRuns')

  cy.intercept('GET', '/api/alert-rules*', {
    body: { items: [], total: 0 },
  }).as('listAlertRules')

  cy.intercept('POST', '/api/alert-rules', (req) => {
    req.reply({
      body: {
        id: 1, name: req.body.name, symbol: req.body.symbol || 'AAPL.US',
        rule_type: req.body.rule_type, threshold: req.body.threshold,
        severity: req.body.severity || 'WARNING', enabled: true,
        cooldown_seconds: req.body.cooldown_seconds || 300,
        last_fired_at: null, created_at: '2026-06-16T12:00:00Z',
      },
    })
  }).as('createAlertRule')

  cy.intercept('POST', '/api/alert-rules/evaluate', {
    body: { evaluated: 0, fired: 0, skipped_cooldown: 0 },
  }).as('evaluateAlertRules')

  cy.intercept('GET', '/api/alert-rules/*/history*', {
    body: {
      items: [
        { id: 1, rule_id: 1, fired_at: '2026-06-16T10:00:00Z', trigger_value: 180, threshold: 175, severity: 'WARNING', message: 'price above 175' },
        { id: 2, rule_id: 1, fired_at: '2026-06-16T10:05:00Z', trigger_value: 185, threshold: 175, severity: 'WARNING', message: 'price above 175' },
        { id: 3, rule_id: 1, fired_at: '2026-06-16T10:10:00Z', trigger_value: 182, threshold: 175, severity: 'WARNING', message: 'price above 175' },
        { id: 4, rule_id: 1, fired_at: '2026-06-16T10:15:00Z', trigger_value: 190, threshold: 175, severity: 'WARNING', message: 'price above 175' },
      ],
      total: 4,
    },
  }).as('getAlertRuleHistory')

  let strategyPresets: Array<{ id: number; name: string; params: Record<string, unknown>; created_at: string }> = []

  cy.intercept('GET', '/api/strategy-presets', (req) => {
    req.reply({ body: { items: strategyPresets, total: strategyPresets.length } })
  }).as('listStrategyPresets')

  cy.intercept('POST', '/api/strategy-presets', (req) => {
    const preset = {
      id: strategyPresets.length + 1,
      name: req.body.name,
      params: req.body.params,
      created_at: '2026-06-16T12:00:00Z',
    }
    strategyPresets.push(preset)
    req.reply({ body: preset })
  }).as('createStrategyPreset')

  cy.intercept('POST', '/api/strategy-presets/*/apply', {
    body: { applied: true, changed: ['buy_low', 'sell_high'] },
  }).as('applyStrategyPreset')

  cy.intercept('POST', '/api/control/start', (req) => {
    status = { ...status, paused: false, kill_switch: false, protective_exit_permitted: false }
    req.reply({ body: { message: 'runner started' } })
  }).as('startAction')

  cy.intercept('POST', '/api/control/stop', (req) => {
    status = { ...status, paused: true, protective_exit_permitted: false }
    req.reply({ body: { message: 'runner stopped' } })
  }).as('stopAction')

  cy.intercept('POST', '/api/control/pause', (req) => {
    status = { ...status, paused: true, protective_exit_permitted: false }
    req.reply({ body: { message: 'trading paused' } })
  }).as('pauseAction')

  cy.intercept('POST', '/api/control/resume', (req) => {
    status = { ...status, paused: false, protective_exit_permitted: false }
    req.reply({ body: { message: 'trading resumed' } })
  }).as('resumeAction')

  cy.intercept('POST', '/api/control/protective-exit/enable', (req) => {
    status = { ...status, protective_exit_permitted: true }
    req.reply({ body: { message: 'protective exits enabled' } })
  }).as('enableProtectiveExitsAction')

  cy.intercept('POST', '/api/control/protective-exit/disable', (req) => {
    status = { ...status, protective_exit_permitted: false }
    req.reply({ body: { message: 'protective exits disabled' } })
  }).as('disableProtectiveExitsAction')

  cy.intercept('POST', '/api/control/kill-switch', (req) => {
    status = { ...status, kill_switch: true, protective_exit_permitted: false }
    req.reply({ body: { message: 'kill switch activated' } })
  }).as('killSwitchAction')

  cy.intercept('POST', '/api/control/disable-kill-switch', (req) => {
    status = { ...status, kill_switch: false }
    req.reply({ body: { message: 'kill switch disabled' } })
  }).as('disableKillSwitchAction')

  cy.intercept('PUT', '/api/strategy', {
    body: {
      id: 1, symbol: 'AAPL.US', market: 'US', buy_low: 100, sell_high: 200,
      short_selling: false, max_daily_loss: 5000, max_drawdown_amount: 250, max_consecutive_losses: 3,
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

  cy.intercept('POST', '/api/credentials/test', {
    body: { ok: true, error: null },
  }).as('testCredentials')

  cy.intercept('POST', '/api/credentials/notification-channels/test', (req) => {
    req.reply({ body: { ok: true, error: null } })
  }).as('testNotificationChannel')

  cy.intercept({ method: 'GET', pathname: '/api/strategy-shadow/config' }, (req) => {
    req.reply({ body: strategyShadowConfig })
  }).as('getStrategyShadowConfig')

  cy.intercept('GET', '/api/strategy-shadow/configs', (req) => {
    req.reply({ body: [strategyShadowConfig] })
  }).as('getStrategyShadowConfigs')

  cy.intercept({ method: 'PUT', pathname: '/api/strategy-shadow/config' }, (req) => {
    strategyShadowConfig = {
      ...strategyShadowConfig,
      ...req.body,
      config_version: 'shadow-stub-v2',
      updated_at: '2026-07-12T02:05:00Z',
    }
    req.reply({ body: strategyShadowConfig })
  }).as('saveStrategyShadowConfig')

  cy.intercept('GET', '/api/strategy-shadow/status*', (req) => {
    req.reply({
      body: {
        config: strategyShadowConfig,
        evidence_config_version: strategyShadowConfig.config_version,
        version_transition_pending: false,
        latest: {
          observed_at: '2026-07-12T02:04:58Z',
          data_age_seconds: 2,
          bar_timestamp_1m: '2026-07-12T02:04:00Z',
          bar_timestamp_5m: '2026-07-12T02:00:00Z',
          price: 210.25,
          vwap_1m: 210.8,
          zscore_1m: -1.15,
          vwap_5m: 211.1,
          zscore_5m: -0.42,
          adx: 18.4,
          realized_vol: 0.0123,
          regime_eligible: true,
          breach_armed: true,
          virtual_position: 'FLAT',
          virtual_entry_price: null,
          virtual_entry_at: null,
          last_action: 'WAIT_RECLAIM',
          last_reason: '等待 1m 价格重新收复 VWAP 残差阈值',
        },
        metrics: strategyShadowMetrics,
        gate_counts: {
          WAIT_BREACH: 32,
          ADX: 15,
          VOL_HIGH: 7,
        },
        phase: 'ARMED_LONG',
        last_polled_at: '2026-07-12T02:04:58Z',
        last_poll_error: '',
      },
    })
  }).as('getStrategyShadowStatus')

  cy.intercept('GET', '/api/strategy-shadow/versions*', {
    body: [
      {
        symbol: 'NVDA.US',
        config_version: 'shadow-stub-v1',
        activated_at: '2026-07-01T13:30:00Z',
        current: true,
        params: {},
        observed_trading_days: 7,
        bars: 120,
        closed_trades: 4,
        net_pnl: 34.2,
      },
      {
        symbol: 'NVDA.US',
        config_version: 'shadow-old-v0',
        activated_at: '2026-06-20T13:30:00Z',
        current: false,
        params: {},
        observed_trading_days: 5,
        bars: 95,
        closed_trades: 2,
        net_pnl: 11.8,
      },
    ],
  }).as('getStrategyShadowVersions')

  cy.intercept('GET', '/api/strategy-shadow/evaluation*', {
    body: {
      symbol: 'NVDA.US',
      config_version: 'shadow-stub-v1',
      mode: 'SHADOW',
      order_submission_allowed: false,
      status: 'COLLECTING',
      observed_trading_days: 7,
      excluded_trading_days: 1,
      minimum_trading_days: 20,
      minimum_session_coverage_ratio: 0.995,
      remaining_trading_days: 13,
      closed_trades: 4,
      eligible_closed_trades: 4,
      excluded_closed_trades: 2,
      minimum_closed_trades: 50,
      remaining_closed_trades: 46,
      first_bar_at: '2026-07-01T13:30:00Z',
      last_bar_at: '2026-07-12T02:04:00Z',
      bars: 120,
      readiness_blockers: [
        'MIN_TRADING_DAYS',
        'MIN_CLOSED_TRADES',
        'COST_STRESS_NET_PNL_NON_POSITIVE',
      ],
      data_quality_warnings: ['2026-07-09: 1 internal bars missing'],
      quality: null,
      daily: [
        {
          session_date: '2026-07-10',
          first_bar_at: '2026-07-10T13:30:00Z',
          last_bar_at: '2026-07-10T19:59:00Z',
          first_ready_at: '2026-07-10T15:49:00Z',
          bars: 390,
          ready_bars: 251,
          warmup_lost_bars: 139,
          eligible_bars: 25,
          hourly_eligibility: [
            {
              session_hour: 9,
              bars: 60,
              ready_bars: 0,
              eligible_bars: 0,
              gate_counts: { ADX_5M_WARMUP: 60 },
            },
            {
              session_hour: 10,
              bars: 60,
              ready_bars: 0,
              eligible_bars: 0,
              gate_counts: { ADX_5M_WARMUP: 60 },
            },
            {
              session_hour: 11,
              bars: 60,
              ready_bars: 41,
              eligible_bars: 5,
              gate_counts: { ZSCORE_5M_NOT_OVERSOLD: 36, ADX_5M_WARMUP: 19 },
            },
          ],
          expected_internal_bars: 390,
          missing_internal_bars: 0,
          incomplete_feature_bars: 0,
          coverage_ratio: 1,
          trades: 2,
          net_pnl: 28.2,
          exit_reasons: { PROFIT_TARGET: 2 },
          partial_start: false,
          partial_end: false,
          outside_session_bars: 0,
          complete_session: true,
        },
      ],
    },
  }).as('getStrategyShadowEvaluation')

  cy.intercept('GET', '/api/strategy-shadow/forward-validation*', (req) => {
    req.reply({ body: strategyShadowForwardValidation })
  }).as('getStrategyShadowForwardValidation')

  cy.intercept('POST', '/api/strategy-shadow/forward-validation/register', (req) => {
    strategyShadowForwardValidation = {
      ...strategyShadowForwardValidation,
      registration: {
        id: 2,
        symbol: req.body.symbol,
        market: 'US',
        market_timezone: 'America/New_York',
        candidate_algorithm_version: req.body.candidate_algorithm_version,
        source_config_version: req.body.source_config_version,
        evaluator_digest: 'frozen123456789012345678901234567890123456789012345678901234567890',
        registered_at: '2026-07-17T12:00:00Z',
        eligible_after: '2026-07-17T13:30:00Z',
        minimum_ready_pairs: 5,
        minimum_mature_pairs: 20,
      },
      status: 'FROZEN',
      included_pairs: 0,
      excluded_targets: 0,
      remaining_ready_pairs: 5,
      remaining_mature_pairs: 20,
      blockers: [],
      daily: [],
    }
    req.reply({ body: strategyShadowForwardValidation })
  }).as('registerStrategyShadowForwardValidation')

  cy.intercept('POST', '/api/strategy-shadow/adx-challengers', (req) => {
    req.reply({
      body: {
        persisted: false,
        mode: 'SHADOW',
        order_submission_allowed: false,
        evaluation_scope: 'EXPLORATORY_IN_SAMPLE',
        promotion_eligible: false,
        forward_validation_required: true,
        symbol: 'NVDA.US',
        source_config_version: req.body.config_version || 'shadow-stub-v1',
        status: 'INSUFFICIENT_EVIDENCE',
        minimum_complete_sessions: 5,
        observed_complete_sessions: 3,
        evaluated_complete_sessions: 3,
        baseline_replay_match: true,
        blockers: ['MIN_COMPLETE_SESSIONS'],
        warmup_diagnostic: {
          algorithm_version: 'strategy-v2-causal-trend-prewarm-v1',
          status: 'INSUFFICIENT_EVIDENCE',
          minimum_causal_pairs: 5,
          observed_causal_pairs: 1,
          evaluated_causal_pairs: 1,
          blockers: ['MIN_CAUSAL_PAIRS'],
          same_sample: true,
          causal_history_only: true,
          vwap_zscore_session_local: true,
          variants: [
            {
              label: 'SESSION_LOCAL',
              warmup_scope: 'NONE',
              source_config_version: req.body.config_version || 'shadow-stub-v1',
              metrics: {
                ...strategyShadowMetrics,
                bars: 390,
                eligible_bars: 25,
              },
              daily: [
                {
                  session_date: '2026-07-10',
                  seed_session_date: '2026-07-09',
                  trend_context_cutoff_at: '2026-07-09T20:00:00Z',
                  overnight_gap_pct: 0.0125,
                  first_ready_at: '2026-07-10T15:49:00Z',
                  bars: 390,
                  ready_bars: 251,
                  warmup_lost_bars: 139,
                  eligible_bars: 25,
                  hourly_eligibility: [
                    {
                      session_hour: 9,
                      bars: 60,
                      ready_bars: 0,
                      eligible_bars: 0,
                      gate_counts: { ADX_5M_WARMUP: 60 },
                    },
                    {
                      session_hour: 10,
                      bars: 60,
                      ready_bars: 0,
                      eligible_bars: 0,
                      gate_counts: { ADX_5M_WARMUP: 60 },
                    },
                    {
                      session_hour: 11,
                      bars: 60,
                      ready_bars: 41,
                      eligible_bars: 5,
                      gate_counts: { ZSCORE_5M_NOT_OVERSOLD: 36, ADX_5M_WARMUP: 19 },
                    },
                  ],
                },
              ],
            },
            {
              label: 'CAUSAL_TREND_PREWARM',
              warmup_scope: 'ADX_VOL_ONLY',
              source_config_version: req.body.config_version || 'shadow-stub-v1',
              metrics: {
                ...strategyShadowMetrics,
                bars: 390,
                eligible_bars: 40,
              },
              daily: [
                {
                  session_date: '2026-07-10',
                  seed_session_date: '2026-07-09',
                  trend_context_cutoff_at: '2026-07-09T20:00:00Z',
                  overnight_gap_pct: 0.0125,
                  first_ready_at: '2026-07-10T14:34:00Z',
                  bars: 390,
                  ready_bars: 326,
                  warmup_lost_bars: 64,
                  eligible_bars: 40,
                  hourly_eligibility: [
                    {
                      session_hour: 9,
                      bars: 60,
                      ready_bars: 0,
                      eligible_bars: 0,
                      gate_counts: { ZSCORE_5M_WARMUP: 60 },
                    },
                    {
                      session_hour: 10,
                      bars: 60,
                      ready_bars: 56,
                      eligible_bars: 8,
                      gate_counts: { ZSCORE_5M_NOT_OVERSOLD: 48, ZSCORE_5M_WARMUP: 4 },
                    },
                    {
                      session_hour: 11,
                      bars: 60,
                      ready_bars: 60,
                      eligible_bars: 12,
                      gate_counts: { ZSCORE_5M_NOT_OVERSOLD: 48 },
                    },
                  ],
                },
              ],
            },
          ],
        },
        candidates: [
          {
            label: 'BASELINE',
            max_adx: 25,
            config_version: req.body.config_version || 'shadow-stub-v1',
            metrics: {
              ...strategyShadowMetrics,
              bars: 1170,
              entries: 4,
            },
            daily: [
              {
                session_date: '2026-07-08',
                bars: 390,
                eligible_bars: 24,
                breaches: 2,
                reclaims: 1,
                closed_trades: 1,
                net_pnl: 12,
                max_drawdown: 0,
                exit_reasons: { PROFIT_TARGET: 1 },
              },
              {
                session_date: '2026-07-09',
                bars: 390,
                eligible_bars: 27,
                breaches: 3,
                reclaims: 2,
                closed_trades: 1,
                net_pnl: -6,
                max_drawdown: 6,
                exit_reasons: { STOP_LOSS: 1 },
              },
              {
                session_date: '2026-07-10',
                bars: 390,
                eligible_bars: 25,
                breaches: 3,
                reclaims: 2,
                closed_trades: 2,
                net_pnl: 28.2,
                max_drawdown: 7.8,
                exit_reasons: { PROFIT_TARGET: 2 },
              },
            ],
          },
          {
            label: 'CHALLENGER',
            max_adx: 20,
            config_version: 'shadow-adx-20',
            metrics: {
              ...strategyShadowMetrics,
              bars: 1170,
              eligible_bars: 54,
              breaches: 6,
              reclaims: 3,
              entries: 2,
              exits: 2,
              closed_trades: 2,
              win_rate: 1,
              gross_pnl: 22.1,
              fees: 2.1,
              net_pnl: 20,
              max_drawdown: 0,
              avg_holding_minutes: 18,
              avg_mae_pct: 0.0025,
              avg_mfe_pct: 0.006,
            },
            daily: [
              {
                session_date: '2026-07-08',
                bars: 390,
                eligible_bars: 18,
                breaches: 2,
                reclaims: 1,
                closed_trades: 1,
                net_pnl: 12,
                max_drawdown: 0,
                exit_reasons: { PROFIT_TARGET: 1 },
              },
              {
                session_date: '2026-07-09',
                bars: 390,
                eligible_bars: 20,
                breaches: 2,
                reclaims: 1,
                closed_trades: 0,
                net_pnl: 0,
                max_drawdown: 0,
                exit_reasons: {},
              },
              {
                session_date: '2026-07-10',
                bars: 390,
                eligible_bars: 16,
                breaches: 2,
                reclaims: 1,
                closed_trades: 1,
                net_pnl: 8,
                max_drawdown: 0,
                exit_reasons: { PROFIT_TARGET: 1 },
              },
            ],
          },
          {
            label: 'CHALLENGER',
            max_adx: 30,
            config_version: 'shadow-adx-30',
            metrics: {
              ...strategyShadowMetrics,
              bars: 1170,
              eligible_bars: 92,
              breaches: 9,
              reclaims: 6,
              entries: 5,
              exits: 5,
              closed_trades: 5,
              win_rate: 0.8,
              gross_pnl: 46.8,
              fees: 5.2,
              net_pnl: 41.6,
              max_drawdown: 8.1,
              avg_holding_minutes: 19.8,
              avg_mae_pct: 0.003,
              avg_mfe_pct: 0.0082,
            },
            daily: [
              {
                session_date: '2026-07-08',
                bars: 390,
                eligible_bars: 30,
                breaches: 3,
                reclaims: 2,
                closed_trades: 2,
                net_pnl: 18,
                max_drawdown: 2,
                exit_reasons: { PROFIT_TARGET: 2 },
              },
              {
                session_date: '2026-07-09',
                bars: 390,
                eligible_bars: 35,
                breaches: 3,
                reclaims: 2,
                closed_trades: 1,
                net_pnl: -3,
                max_drawdown: 8.1,
                exit_reasons: { STOP_LOSS: 1 },
              },
              {
                session_date: '2026-07-10',
                bars: 390,
                eligible_bars: 27,
                breaches: 3,
                reclaims: 2,
                closed_trades: 2,
                net_pnl: 26.6,
                max_drawdown: 5.4,
                exit_reasons: { PROFIT_TARGET: 2 },
              },
            ],
          },
        ],
      },
    })
  }).as('evaluateStrategyShadowAdxChallengers')

  cy.intercept('GET', '/api/strategy-shadow/decisions*', {
    body: {
      items: [
        {
          id: 1,
          symbol: 'NVDA.US',
          config_version: 'shadow-stub-v1',
          observed_at: '2026-07-12T02:04:58Z',
          bar_timestamp_1m: '2026-07-12T02:04:00Z',
          bar_timestamp_5m: '2026-07-12T02:00:00Z',
          price: 210.25,
          vwap_1m: 210.8,
          zscore_1m: -1.15,
          vwap_5m: 211.1,
          zscore_5m: -0.42,
          adx: 18.4,
          realized_vol: 0.0123,
          regime_eligible: true,
          breach_armed: true,
          action: 'WAIT_RECLAIM',
          reason: '等待收复',
          virtual_position: 'FLAT',
          reference_price: 210.25,
          quantity: 0,
          gross_pnl: null,
          fee: null,
          net_pnl: null,
          exit_reason: null,
          holding_minutes: null,
          mae_pct: null,
          mfe_pct: null,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    },
  }).as('getStrategyShadowDecisions')

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
