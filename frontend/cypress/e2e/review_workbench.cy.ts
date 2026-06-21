function ensureClipboard(win: Window): Navigator['clipboard'] {
  const existing = win.navigator.clipboard
  if (existing) return existing

  const clipboard = {
    writeText: () => Promise.resolve(),
  } as unknown as Navigator['clipboard']

  Object.defineProperty(win.navigator, 'clipboard', {
    configurable: true,
    value: clipboard,
  })

  return clipboard
}

function parseCsvLine(line: string): string[] {
  const cells: string[] = []
  let cell = ''
  let inQuotes = false

  for (let i = 0; i < line.length; i += 1) {
    const char = line[i]
    const next = line[i + 1]

    if (char === '"') {
      if (inQuotes && next === '"') {
        cell += '"'
        i += 1
      } else {
        inQuotes = !inQuotes
      }
      continue
    }

    if (char === ',' && !inQuotes) {
      cells.push(cell)
      cell = ''
      continue
    }

    cell += char
  }

  cells.push(cell)
  return cells
}

describe('Review Workbench Enhancements', () => {
  it('persists review workbench preferences across reload', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: -10,
              trade_count: 0,
              error_tags: ['RISK'],
            },
          ],
          total_pnl: -10,
          total_trades: 0,
          all_error_tags: ['RISK'],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: -10, consecutive_losses: 1 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-day-filter-losing"]').click()
    cy.get('[data-testid="review-visible-day-count"]').should('contain', '1 / 1 天')
    cy.get('[data-testid="review-keyword-filter"]').invoke('val', 'RISK').trigger('input').trigger('change')
    cy.get('[data-testid="review-compact-mode"]').click()

    cy.reload()

    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-day-filter-losing"]').should('have.class', 'el-button--primary')
    cy.get('[data-testid="review-keyword-filter"]').should('have.value', 'RISK')
    cy.get('[data-testid="review-page-root"]').should('have.class', 'review-compact')
  })

  it('shows review health score for current query', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [
                {
                  id: 101,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: true,
                  order_action: 'BUY',
                  order_status: 'FILLED',
                  order_id: 'lb-review-101',
                  applied: true,
                  created_at: '2026-06-01T10:00:00Z',
                },
                {
                  id: 105,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: true,
                  order_action: 'HOLD',
                  order_status: 'SKIPPED',
                  order_id: null,
                  applied: false,
                  created_at: '2026-06-01T10:03:00Z',
                },
              ],
              orders: [
                {
                  id: 201,
                  broker_order_id: 'lb-review-101',
                  symbol: 'AAPL.US',
                  side: 'BUY',
                  quantity: 1,
                  price: 190,
                  executed_quantity: 1,
                  executed_price: 191,
                  status: 'FILLED',
                  created_at: '2026-06-01T10:01:00Z',
                  filled_at: '2026-06-01T10:02:00Z',
                },
              ],
              events: [],
              snapshots: [],
              daily_pnl: 15,
              trade_count: 1,
              error_tags: [],
            },
            {
              date: '2026-06-02',
              symbol: 'AAPL.US',
              llm_interactions: [
                {
                  id: 102,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: false,
                  order_action: 'BUY',
                  order_status: 'REJECTED',
                  order_id: 'lb-review-102',
                  applied: false,
                  created_at: '2026-06-02T10:00:00Z',
                },
              ],
              orders: [
                {
                  id: 202,
                  broker_order_id: 'lb-review-rejected',
                  symbol: 'AAPL.US',
                  side: 'SELL',
                  quantity: 1,
                  price: 192,
                  executed_quantity: null,
                  executed_price: null,
                  status: 'REJECTED',
                  created_at: '2026-06-02T10:01:00Z',
                  filled_at: null,
                },
              ],
              events: [
                {
                  id: 301,
                  event_type: 'ORDER_REJECTED',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-rejected',
                  side: 'SELL',
                  status: 'REJECTED',
                  message: 'order rejected',
                  payload_json: '{"skip_category":"RISK"}',
                  created_at: '2026-06-02T10:01:30Z',
                },
                {
                  id: 302,
                  event_type: 'RISK_PAUSED',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-risk',
                  side: 'SELL',
                  status: 'PAUSED',
                  message: 'risk paused',
                  payload_json: '{"skip_category":"RISK"}',
                  created_at: '2026-06-02T10:02:00Z',
                },
                {
                  id: 303,
                  event_type: 'ORDER_CANCELLED',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-broker',
                  side: 'SELL',
                  status: 'CANCELLED',
                  message: 'broker issue',
                  payload_json: '{"skip_category":"BROKER"}',
                  created_at: '2026-06-02T10:03:00Z',
                },
                {
                  id: 304,
                  event_type: 'SESSION_BLOCKED',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-session',
                  side: 'SELL',
                  status: 'BLOCKED',
                  message: 'session blocked',
                  payload_json: '{"skip_category":RISK',
                  created_at: '2026-06-02T10:04:00Z',
                },
              ],
              snapshots: [
                {
                  id: 401,
                  engine_state: 'FLAT',
                  daily_pnl: -10,
                  consecutive_losses: 2,
                  last_price: 97,
                  last_trigger_price: 101,
                  created_at: '2026-06-02T10:02:00Z',
                },
              ],
              daily_pnl: -40,
              trade_count: 1,
              error_tags: ['RISK', 'BROKER', 'SESSION'],
            },
            {
              date: '2026-06-03',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: -25,
          total_trades: 2,
          all_error_tags: ['RISK', 'BROKER', 'SESSION'],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: -25, consecutive_losses: 2 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('[data-testid="review-copy-brief"]').should('contain', '复制摘要').and('be.disabled')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-03').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-health-score"]').should('contain', '复盘健康').and('contain', '高风险').and('contain', '当前查询结果')
    cy.get('[data-testid="review-health-reasons"]').should('contain', '区间亏损').and('contain', '存在错误').and('contain', '订单异常')
    cy.get('[data-testid="review-health-score"]').should('contain', '20')
  })

  it('ignores invalid review workbench localStorage without crashing', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-01',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: 0,
          total_trades: 0,
          all_error_tags: [],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 0, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review', {
      onBeforeLoad(win) {
        win.localStorage.setItem('auto_trade.review.workbench.v1', '{invalid-json')
      },
    })

    cy.get('[data-testid="review-page-root"]').should('be.visible')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()
    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')
    cy.get('[data-testid="review-keyword-filter"]').should('have.value', '')
  })

  it('copies a concise review brief', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [
                {
                  id: 101,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: true,
                  order_action: 'BUY',
                  order_status: 'FILLED',
                  order_id: 'lb-review-101',
                  applied: true,
                  created_at: '2026-06-01T10:00:00Z',
                },
              ],
              orders: [
                {
                  id: 201,
                  broker_order_id: 'lb-review-101',
                  symbol: 'AAPL.US',
                  side: 'BUY',
                  quantity: 1,
                  price: 190,
                  executed_quantity: 1,
                  executed_price: 191,
                  status: 'FILLED',
                  created_at: '2026-06-01T10:01:00Z',
                  filled_at: '2026-06-01T10:02:00Z',
                },
              ],
              events: [],
              snapshots: [],
              daily_pnl: 15,
              trade_count: 1,
              error_tags: [],
            },
            {
              date: '2026-06-02',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [
                {
                  id: 202,
                  broker_order_id: 'lb-review-rejected',
                  symbol: 'AAPL.US',
                  side: 'SELL',
                  quantity: 1,
                  price: 192,
                  executed_quantity: null,
                  executed_price: null,
                  status: 'REJECTED',
                  created_at: '2026-06-02T10:01:00Z',
                  filled_at: null,
                },
              ],
              events: [],
              snapshots: [],
              daily_pnl: -40,
              trade_count: 1,
              error_tags: ['RISK'],
            },
          ],
          total_pnl: -25,
          total_trades: 2,
          all_error_tags: ['RISK'],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: -25, consecutive_losses: 2 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-03').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-day-filter-losing"]').click()
    cy.contains('当前筛选结果：1 / 2 天').should('be.visible')

    cy.get('[data-testid="review-copy-brief"]').should('contain', '复制摘要').and('not.be.disabled')

    cy.window().then((win) => {
      const clipboard = ensureClipboard(win)
      const writeTextStub = cy.stub(clipboard, 'writeText').resolves()
      cy.wrap(writeTextStub).as('writeText')
    })

    cy.get('[data-testid="review-copy-brief"]').click()
    cy.get('@writeText').should('have.been.calledOnce')
    cy.get('@writeText')
      .its('firstCall.args.0')
      .should(
        'eq',
        [
          '摘要：AAPL.US · 2026-06-01 ~ 2026-06-03',
          '复盘健康：需关注 50',
          '总盈亏：-$25.00 · 交易：2 · 当前筛选天数：1',
          '错误标签：RISK',
          'LLM 动作：无（成功 0/0，已应用 0）',
          '订单质量：成交 0，部分 0，挂起 0，异常 1，平均滑点 -',
        ].join('\n'),
      )
    cy.contains('复盘摘要已复制').should('be.visible')
  })

  it('keeps copy brief disabled when review data is empty', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-01',
          days: [],
          total_pnl: 0,
          total_trades: 0,
          all_error_tags: [],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 0, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('[data-testid="review-copy-brief"]').should('contain', '复制摘要').and('be.disabled')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-copy-brief"]').should('contain', '复制摘要').and('be.disabled')
  })

  it('exports visible review timeline rows as local CSV', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 10,
              trade_count: 0,
              error_tags: [],
            },
            {
              date: '2026-06-02',
              symbol: 'AAPL.US',
              llm_interactions: [
                {
                  id: 501,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: true,
                  order_action: 'BUY',
                  order_status: 'FILLED',
                  order_id: 'lb-review-501',
                  applied: true,
                  created_at: '2026-06-02T09:00:00Z',
                },
              ],
              orders: [
                {
                  id: 202,
                  broker_order_id: 'lb-review-rejected',
                  symbol: 'AAPL.US',
                  side: 'SELL',
                  quantity: 1,
                  price: 192,
                  executed_quantity: null,
                  executed_price: null,
                  status: 'REJECTED',
                  created_at: '2026-06-02T10:01:00Z',
                  filled_at: null,
                },
              ],
              events: [
                {
                  id: 301,
                  event_type: 'ORDER_REJECTED',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-rejected',
                  side: 'SELL',
                  status: 'REJECTED',
                  message: '=cmd|\' /C calc\'!A0',
                  payload_json: '{"skip_category":"RISK","note":"@risk"}',
                  created_at: '2026-06-02T10:01:30Z',
                },
                {
                  id: 305,
                  event_type: 'SESSION_BLOCKED',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-bad-payload',
                  side: 'SELL',
                  status: 'BLOCKED',
                  message: 'bad payload',
                  payload_json: '{"skip_category":RISK',
                  created_at: '2026-06-02T10:01:45Z',
                },
              ],
              snapshots: [
                {
                  id: 401,
                  engine_state: 'FLAT',
                  daily_pnl: -10,
                  consecutive_losses: 2,
                  last_price: 97,
                  last_trigger_price: 101,
                  created_at: '2026-06-02T10:02:00Z',
                },
              ],
              daily_pnl: -40,
              trade_count: 1,
              error_tags: ['RISK'],
            },
            {
              date: '2026-06-03',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 5,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: -25,
          total_trades: 2,
          all_error_tags: ['RISK'],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: -25, consecutive_losses: 2 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-03').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-day-filter-losing"]').click()
    cy.contains('当前筛选结果：1 / 3 天').should('be.visible')
    cy.get('[data-testid="review-day-filter-winning"]').click()
    cy.contains('当前筛选结果：2 / 3 天').should('be.visible')
    cy.contains('2026-06-01').should('be.visible')
    cy.contains('2026-06-03').should('be.visible')
    cy.get('[data-testid="review-day-filter-losing"]').click()
    cy.contains('当前筛选结果：1 / 3 天').should('be.visible')
    cy.exec('rm -f cypress/downloads/review_visible_timeline.csv')
    cy.get('[data-testid="review-export-visible-csv"]').should('not.be.disabled').click()
    cy.contains('已导出当前筛选复盘 CSV').should('be.visible')
    cy.readFile('cypress/downloads/review_visible_timeline.csv', 'utf8').then((csv) => {
      const normalized = csv.replace(/^\uFEFF/, '')
      expect(normalized.startsWith('日期,来源,标的,类型,状态,方向,消息,盈亏,时间,订单号')).to.eq(true)
      expect(normalized).to.contain('2026-06-02')
      expect(normalized).to.not.contain('2026-06-01')
      expect(normalized).to.not.contain('2026-06-03')
      expect(normalized).to.contain("'=cmd|' /C calc'!A0")
      expect(normalized).to.contain('payload {skip_category: RISK, note: @risk}')
      const lines = normalized.trim().split('\n')
      expect(lines).to.have.length(6)
      expect(lines[1]).to.contain('llm')
      expect(lines[2]).to.contain('order')
      expect(lines[3]).to.contain('event')
      expect(lines[4]).to.contain('event')
      expect(lines[5]).to.contain('snapshot')
      expect(lines[1]).to.contain('09:00:00')
      expect(lines[2]).to.contain('10:01:00')
      expect(lines[3]).to.contain('10:01:30')
      expect(lines[4]).to.contain('10:01:45')
      expect(lines[5]).to.contain('10:02:00')
      const badPayloadRow = parseCsvLine(lines[4])
      expect(badPayloadRow[6]).to.eq('bad payload {"skip_category":RISK')
    })
  })

  it('keeps export disabled when no visible timeline rows exist', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-01',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: 0,
          total_trades: 0,
          all_error_tags: [],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 0, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-export-visible-csv"]').should('be.disabled')
  })

  it('shows copy failure message when clipboard is rejected', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-01',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: 0,
          total_trades: 0,
          all_error_tags: [],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 0, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.window().then((win) => {
      const clipboard = ensureClipboard(win)
      const writeTextStub = cy.stub(clipboard, 'writeText').rejects(new Error('denied'))
      cy.wrap(writeTextStub).as('writeText')
    })

    cy.get('[data-testid="review-copy-brief"]').click()
    cy.get('@writeText').should('have.been.calledOnce')
    cy.contains('复盘摘要已复制').should('not.exist')
    cy.contains('复制失败，请检查浏览器剪贴板权限').should('be.visible')
  })

  it('does not mark single error tag with non-failed order as high risk', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-01',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [
                {
                  id: 201,
                  broker_order_id: 'lb-review-submitted',
                  symbol: 'AAPL.US',
                  side: 'BUY',
                  quantity: 1,
                  price: 190,
                  executed_quantity: null,
                  executed_price: null,
                  status: 'SUBMITTED',
                  created_at: '2026-06-01T10:01:00Z',
                  filled_at: null,
                },
              ],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 1,
              error_tags: ['RISK'],
            },
          ],
          total_pnl: 0,
          total_trades: 1,
          all_error_tags: ['RISK'],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 0, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-health-score"]').should('contain', '健康').and('not.contain', '高风险')
    cy.get('[data-testid="review-health-score"]').should('contain', '90')
    cy.get('[data-testid="review-health-reasons"]').should('contain', '存在错误').and('not.contain', '订单异常')
  })

  it('filters review days by quick day filters and resets filters', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 1,
              error_tags: [],
            },
            {
              date: '2026-06-02',
              symbol: 'AAPL.US',
              llm_interactions: [
                {
                  id: 102,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: false,
                  order_action: 'BUY',
                  order_status: 'REJECTED',
                  order_id: 'lb-review-102',
                  applied: false,
                  created_at: '2026-06-02T10:00:00Z',
                },
              ],
              orders: [],
              events: [
                {
                  id: 301,
                  event_type: 'ORDER_REJECTED',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-rejected',
                  side: 'SELL',
                  status: 'REJECTED',
                  message: 'order rejected',
                  payload_json: '{"skip_category":"RISK"}',
                  created_at: '2026-06-02T10:01:30Z',
                },
              ],
              snapshots: [],
              daily_pnl: -40,
              trade_count: 1,
              error_tags: ['RISK'],
            },
            {
              date: '2026-06-03',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 20,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: -30,
          total_trades: 2,
          all_error_tags: ['RISK'],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: -30, consecutive_losses: 1 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-03').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-visible-day-count"]').should('contain', '3 / 3')
    cy.get('[data-testid="review-day-filter-all"]').should('have.class', 'el-button--primary')

    cy.get('[data-testid="review-day-filter-losing"]').click()
    cy.get('[data-testid="review-day-filter-losing"]').should('have.class', 'el-button--primary')
    cy.get('[data-testid="review-day-filter-all"]').should('not.have.class', 'el-button--primary')
    cy.get('[data-testid="review-visible-day-count"]').should('contain', '1 / 3')
    cy.get('[data-testid="review-day-card-2026-06-02"]').should('exist')
    cy.get('[data-testid="review-day-card-2026-06-01"]').should('not.exist')

    cy.get('[data-testid="review-day-filter-error"]').click()
    cy.get('[data-testid="review-day-filter-error"]').should('have.class', 'el-button--primary')
    cy.get('[data-testid="review-visible-day-count"]').should('contain', '1 / 3')
    cy.get('[data-testid="review-day-card-2026-06-02"]').should('exist')

    cy.get('[data-testid="review-day-filter-all"]').click()
    cy.get('[data-testid="review-day-filter-all"]').should('have.class', 'el-button--primary')
    cy.get('[data-testid="review-day-filter-no-trade"]').click()
    cy.get('[data-testid="review-day-filter-no-trade"]').should('have.class', 'el-button--primary')
    cy.get('[data-testid="review-visible-day-count"]').should('contain', '1 / 3')
    cy.get('[data-testid="review-day-card-2026-06-03"]').should('exist')

    cy.get('[data-testid="review-reset-workbench-filters"]').click()
    cy.get('[data-testid="review-day-filter-all"]').should('have.class', 'el-button--primary')
    cy.get('[data-testid="review-visible-day-count"]').should('contain', '3 / 3')
  })

  it('filters current review timeline by keyword and shows filtered empty state', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [
                {
                  id: 301,
                  event_type: 'ORDER_SUBMITTED',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-001',
                  side: 'BUY',
                  status: 'SUBMITTED',
                  message: 'regular submit',
                  payload_json: '{"skip_category":"POSITION"}',
                  created_at: '2026-06-01T10:01:30Z',
                },
              ],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 1,
              error_tags: [],
            },
            {
              date: '2026-06-02',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [
                {
                  id: 302,
                  event_type: 'BROKER_RETRY',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-002',
                  side: 'SELL',
                  status: 'RETRYING',
                  message: 'broker retry scheduled',
                  payload_json: '{"skip_category":"BROKER_RETRY"}',
                  created_at: '2026-06-02T10:01:30Z',
                },
              ],
              snapshots: [
                {
                  id: 401,
                  engine_state: 'FLAT',
                  daily_pnl: -10,
                  consecutive_losses: 2,
                  last_price: 97,
                  last_trigger_price: 101,
                  created_at: '2026-06-02T10:02:00Z',
                },
              ],
              daily_pnl: -10,
              trade_count: 1,
              error_tags: ['BROKER_RETRY'],
            },
            {
              date: '2026-06-03',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: -10,
          total_trades: 2,
          all_error_tags: ['BROKER_RETRY'],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: -10, consecutive_losses: 1 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-03').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-keyword-filter"]').type('BROKER_RETRY')
    cy.get('[data-testid="review-day-card-2026-06-02"]').should('exist')
    cy.get('[data-testid="review-day-card-2026-06-01"]').should('not.exist')

    cy.get('[data-testid="review-keyword-filter"]').clear().type('FLAT')
    cy.get('[data-testid="review-day-card-2026-06-02"]').should('exist')
    cy.get('[data-testid="review-day-card-2026-06-01"]').should('not.exist')

    cy.get('[data-testid="review-keyword-filter"]').clear().type('not-found-keyword')
    cy.get('[data-testid="review-filtered-empty"]').should('be.visible').and('contain', '当前筛选条件下无复盘日')
  })

  it('summarizes LLM actions for visible review days', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [
                {
                  id: 101,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: true,
                  order_action: 'BUY',
                  order_status: 'FILLED',
                  order_id: 'lb-review-101',
                  applied: true,
                  created_at: '2026-06-01T10:00:00Z',
                },
                {
                  id: 102,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: false,
                  order_action: 'SELL',
                  order_status: 'REJECTED',
                  order_id: 'lb-review-102',
                  applied: false,
                  created_at: '2026-06-01T10:05:00Z',
                },
              ],
              orders: [
                {
                  id: 201,
                  broker_order_id: 'lb-review-101',
                  symbol: 'AAPL.US',
                  side: 'BUY',
                  quantity: 1,
                  price: 190,
                  executed_quantity: 1,
                  executed_price: 191,
                  status: 'FILLED',
                  created_at: '2026-06-01T10:01:00Z',
                  filled_at: '2026-06-01T10:02:00Z',
                },
              ],
              events: [],
              snapshots: [],
              daily_pnl: 15,
              trade_count: 1,
              error_tags: [],
            },
            {
              date: '2026-06-02',
              symbol: 'AAPL.US',
              llm_interactions: [
                {
                  id: 103,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: true,
                  order_action: 'NONE',
                  order_status: 'SKIPPED',
                  order_id: null,
                  applied: false,
                  created_at: '2026-06-02T10:00:00Z',
                },
                {
                  id: 104,
                  interaction_type: 'analyze',
                  symbol: 'AAPL.US',
                  market: 'US',
                  success: true,
                  order_action: 'HOLD',
                  order_status: 'SKIPPED',
                  order_id: null,
                  applied: false,
                  created_at: '2026-06-02T10:01:00Z',
                },
              ],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
            {
              date: '2026-06-03',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: 15,
          total_trades: 1,
          all_error_tags: [],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 15, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-03').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-llm-action-summary"]').should('contain', 'LLM 动作摘要（当前筛选结果）')
      .and('contain', 'BUY 1')
      .and('contain', 'SELL 1')
      .and('contain', 'NONE 1')
      .and('contain', 'other')
      .and('contain', '成功')
      .and('contain', '已应用 1')

    cy.get('[data-testid="review-llm-action-summary"] .section-block')
      .first()
      .find('.el-tag')
      .should('have.length', 6)
      .then(($tags) => {
        expect($tags.eq(0)).to.contain('BUY 1')
        expect($tags.eq(1)).to.contain('SELL 1')
        expect($tags.eq(2)).to.contain('SHORT 0')
        expect($tags.eq(3)).to.contain('COVER 0')
        expect($tags.eq(4)).to.contain('NONE 1')
        expect($tags.eq(5)).to.contain('other 1')
      })

    cy.get('[data-testid="review-day-filter-llm"]').click()
    cy.get('[data-testid="review-llm-action-summary"]').should('contain', '成功')

    cy.get('[data-testid="review-keyword-filter"]').clear().type('NONE')
    cy.get('[data-testid="review-llm-action-summary"]').should('contain', 'NONE 1')
      .and('contain', '成功 2/2')
      .and('contain', '已应用 0')
  })

  it('shows snapshot volatility strip for visible days', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [
                {
                  id: 401,
                  engine_state: 'FLAT',
                  daily_pnl: 3,
                  consecutive_losses: 1,
                  last_price: 97,
                  last_trigger_price: 101,
                  created_at: '2026-06-01T10:02:00Z',
                },
                {
                  id: 402,
                  engine_state: 'LONG',
                  daily_pnl: -1,
                  consecutive_losses: 3,
                  last_price: '110',
                  last_trigger_price: 0,
                  created_at: '2026-06-01T10:03:00Z',
                },
                {
                  id: 403,
                  engine_state: 'SHORT',
                  daily_pnl: 2,
                  consecutive_losses: 2,
                  last_price: '96.999',
                  last_trigger_price: 0,
                  created_at: 'invalid-time',
                },
                {
                  id: 404,
                  engine_state: 'HALT',
                  daily_pnl: 0,
                  consecutive_losses: 0,
                  last_price: 'Infinity',
                  last_trigger_price: 'Infinity',
                  created_at: '2026-06-01T10:04:00Z',
                },
                {
                  id: 405,
                  engine_state: 'PAUSE',
                  daily_pnl: 0,
                  consecutive_losses: 0,
                  last_price: '',
                  last_trigger_price: null,
                  created_at: '2026-06-01T10:05:00Z',
                },
              ],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
            {
              date: '2026-06-02',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [
                {
                  id: 402,
                  engine_state: 'LONG',
                  daily_pnl: -4,
                  consecutive_losses: 2,
                  last_price: 101,
                  last_trigger_price: 97,
                  created_at: '2026-06-02T10:02:00Z',
                },
              ],
              daily_pnl: -10,
              trade_count: 1,
              error_tags: ['RISK'],
            },
            {
              date: '2026-06-03',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 1,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: -10,
          total_trades: 1,
          all_error_tags: ['RISK'],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: -10, consecutive_losses: 2 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-03').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-snapshot-strip"]').should('contain', '样本 6').and('contain', '最低 96.99').and('contain', '最高 110').and('contain', '最大连亏 3').and('contain', '触发距离 4').and('contain', '最新状态 LONG')
    cy.get('[data-testid="review-snapshot-detail"]').should('contain', '$Infinity')

    cy.get('[data-testid="review-keyword-filter"]').clear().type('2026-06-02')
    cy.get('[data-testid="review-snapshot-strip"]').should('contain', '样本 1').and('contain', '最低 101').and('contain', '最高 101').and('contain', '最大连亏 2').and('contain', '触发距离 4').and('contain', '最新状态 LONG')
  })

  it('keeps snapshot ties stable at same created_at and ignores invalid numeric samples', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-01',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [
                {
                  id: 501,
                  engine_state: 'FIRST',
                  daily_pnl: 1,
                  consecutive_losses: 1,
                  last_price: 'NaN',
                  last_trigger_price: null,
                  created_at: '2026-06-01T10:00:00Z',
                },
                {
                  id: 502,
                  engine_state: 'SECOND',
                  daily_pnl: 2,
                  consecutive_losses: 2,
                  last_price: '96.25',
                  last_trigger_price: '100.25',
                  created_at: '2026-06-01T10:00:00Z',
                },
                {
                  id: 503,
                  engine_state: 'THIRD',
                  daily_pnl: 3,
                  consecutive_losses: 3,
                  last_price: null,
                  last_trigger_price: '',
                  created_at: '2026-06-01T09:59:00Z',
                },
              ],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: 0,
          total_trades: 0,
          all_error_tags: [],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 0, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-snapshot-strip"]').should('contain', '样本 3').and('contain', '最低 96.25').and('contain', '最高 96.25').and('contain', '触发距离 4').and('contain', '最新状态 SECOND').and('not.contain', 'NaN').and('not.contain', 'Infinity')
  })

  it('truncates snapshot trigger distance to two decimals for 1.999 samples', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-01',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [
                {
                  id: 504,
                  engine_state: 'TRUNCATE',
                  daily_pnl: 0,
                  consecutive_losses: 0,
                  last_price: 98.001,
                  last_trigger_price: 100,
                  created_at: '2026-06-01T10:00:00Z',
                },
              ],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: 0,
          total_trades: 0,
          all_error_tags: [],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 0, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-snapshot-strip"]').should('contain', '触发距离 1.99').and('not.contain', '触发距离 2')
  })

  it('shows empty snapshot strip when no samples remain', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-01',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: 0,
          total_trades: 0,
          all_error_tags: [],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 0, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-snapshot-strip"]').should('contain', '无快照样本')
  })

  it('summarizes order execution quality for visible review days', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [
                {
                  id: 201,
                  broker_order_id: 'lb-review-filled',
                  symbol: 'AAPL.US',
                  side: 'BUY',
                  quantity: 1,
                  price: 190,
                  executed_quantity: 1,
                  executed_price: 191,
                  status: 'FILLED',
                  created_at: '2026-06-01T10:01:00Z',
                  filled_at: '2026-06-01T10:02:00Z',
                },
              ],
              events: [],
              snapshots: [],
              daily_pnl: 10,
              trade_count: 1,
              error_tags: [],
            },
            {
              date: '2026-06-02',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [
                {
                  id: 202,
                  broker_order_id: 'lb-review-partial',
                  symbol: 'AAPL.US',
                  side: 'SELL',
                  quantity: 2,
                  price: 192,
                  executed_quantity: 1,
                  executed_price: 193,
                  status: 'PARTIAL_FILLED',
                  created_at: '2026-06-02T10:01:00Z',
                  filled_at: '2026-06-02T10:02:00Z',
                },
                {
                  id: 203,
                  broker_order_id: 'lb-review-cancelled-partial',
                  symbol: 'AAPL.US',
                  side: 'SELL',
                  quantity: 2,
                  price: 195,
                  executed_quantity: 1,
                  executed_price: 197,
                  status: 'CANCELLED',
                  created_at: '2026-06-02T10:03:00Z',
                  filled_at: '2026-06-02T10:04:00Z',
                },
                {
                  id: 204,
                  broker_order_id: 'lb-review-timeout',
                  symbol: 'AAPL.US',
                  side: 'SELL',
                  quantity: 1,
                  price: 197,
                  executed_quantity: null,
                  executed_price: null,
                  status: 'TIMEOUT',
                  created_at: '2026-06-02T10:05:00Z',
                  filled_at: null,
                },
                {
                  id: 205,
                  broker_order_id: 'lb-review-error',
                  symbol: 'AAPL.US',
                  side: 'SELL',
                  quantity: 1,
                  price: 198,
                  executed_quantity: null,
                  executed_price: null,
                  status: 'ERROR',
                  created_at: '2026-06-02T10:06:00Z',
                  filled_at: null,
                },
                {
                  id: 206,
                  broker_order_id: 'lb-review-rejected',
                  symbol: 'AAPL.US',
                  side: 'SELL',
                  quantity: 1,
                  price: 194,
                  executed_quantity: null,
                  executed_price: null,
                  status: 'REJECTED',
                  created_at: '2026-06-02T10:03:00Z',
                  filled_at: null,
                },
              ],
              events: [],
              snapshots: [],
              daily_pnl: -5,
              trade_count: 2,
              error_tags: ['RISK'],
            },
            {
              date: '2026-06-03',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: 5,
          total_trades: 3,
          all_error_tags: ['RISK'],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 5, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-03').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-execution-quality"]')
      .should('contain', '订单执行质量（当前筛选结果）')
      .and('contain', '成交 1')
      .and('contain', '部分 2')
      .and('contain', '异常 3')
      .and('contain', '平均价差(成交-委托) 1.33')
      .and('contain', '样本 3')

    cy.get('[data-testid="review-day-filter-losing"]').click()
    cy.get('[data-testid="review-execution-quality"]')
      .should('contain', '成交 0')
      .and('contain', '部分 2')
      .and('contain', '异常 3')
      .and('contain', '平均价差(成交-委托) 1.50')
      .and('contain', '样本 2')
  })

  it('groups visible events into deterministic triage buckets', () => {
    cy.stubApi()

    cy.intercept('GET', '/api/review*', (req) => {
      req.reply({
        body: {
          symbol: 'AAPL.US',
          from_date: '2026-06-01',
          to_date: '2026-06-03',
          days: [
            {
              date: '2026-06-01',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [
                {
                  id: 401,
                  event_type: 'risk_pause',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-401',
                  side: 'BUY',
                  status: 'PAUSED',
                  message: '',
                  payload_json: '{"tag":"risk"}',
                  created_at: '2026-06-01T10:00:00Z',
                },
                {
                  id: 402,
                  event_type: 'order_reject',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-402',
                  side: 'BUY',
                  status: 'REJECTED',
                  message: 'cooldown triggered after reject',
                  payload_json: '{"tag":"order"}',
                  created_at: '2026-06-01T10:01:00Z',
                },
                {
                  id: 403,
                  event_type: 'session_rth_block',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-403',
                  side: 'BUY',
                  status: 'INFO',
                  message: 'market open info',
                  payload_json: '{"tag":"session"}',
                  created_at: '2026-06-01T10:02:00Z',
                },
              ],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
            {
              date: '2026-06-02',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [
                {
                  id: 404,
                  event_type: 'broker_quote_stream',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-404',
                  side: 'SELL',
                  status: 'STREAMING',
                  message: 'broker_retry later',
                  payload_json: '{"tag":"broker"}',
                  created_at: '2026-06-02T10:00:00Z',
                },
                {
                  id: 405,
                  event_type: 'llm_interval_advisor',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-405',
                  side: 'SELL',
                  status: 'OK',
                  message: 'interval advisor result',
                  payload_json: '{"tag":"llm"}',
                  created_at: '2026-06-02T10:01:00Z',
                },
                {
                  id: 406,
                  event_type: 'misc_note',
                  symbol: 'AAPL.US',
                  broker_order_id: null,
                  side: null,
                  status: 'idle',
                  message: '',
                  payload_json: '{}',
                  created_at: '2026-06-02T10:02:00Z',
                },
                {
                  id: 407,
                  event_type: '',
                  symbol: 'AAPL.US',
                  broker_order_id: null,
                  side: null,
                  status: 'REJECTED',
                  message: null,
                  payload_json: null,
                  created_at: '2026-06-02T10:03:00Z',
                },
                {
                  id: 408,
                  event_type: 'BROKER_ERROR',
                  symbol: 'AAPL.US',
                  broker_order_id: null,
                  side: null,
                  status: null,
                  message: null,
                  payload_json: null,
                  created_at: '2026-06-02T10:04:00Z',
                },
                {
                  id: 409,
                  event_type: 'LLM_ERROR',
                  symbol: 'AAPL.US',
                  broker_order_id: null,
                  side: null,
                  status: null,
                  message: null,
                  payload_json: null,
                  created_at: '2026-06-02T10:05:00Z',
                },
                {
                  id: 410,
                  event_type: 'ORDER_REJECTED',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-410',
                  side: 'BUY',
                  status: 'REJECTED',
                  message: 'RISK gate tripped after order reject',
                  payload_json: '{"skip_category":"ORDER","reason":"RISK"}',
                  created_at: '2026-06-02T10:06:00Z',
                },
                {
                  id: 411,
                  event_type: 'BROKER_ERROR',
                  symbol: 'AAPL.US',
                  broker_order_id: 'lb-review-411',
                  side: 'SELL',
                  status: 'ERROR',
                  message: 'order rejected by broker',
                  payload_json: '{"skip_category":"ORDER","reason":"BROKER"}',
                  created_at: '2026-06-02T10:07:00Z',
                },
              ],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
            {
              date: '2026-06-03',
              symbol: 'AAPL.US',
              llm_interactions: [],
              orders: [],
              events: [],
              snapshots: [],
              daily_pnl: 0,
              trade_count: 0,
              error_tags: [],
            },
          ],
          total_pnl: 0,
          total_trades: 0,
          all_error_tags: [],
        },
      })
    }).as('getReview')

    cy.intercept('GET', '/api/status/history*', { body: { points: [], markers: [] } }).as('getStatusHistory')
    cy.intercept('GET', '/api/diagnostics', {
      body: {
        runner_running: true,
        thread_alive: true,
        quotes_subscribed: true,
        trigger_in_flight: false,
        pending_order_symbols: [],
        quote_stream: { last_push_age_seconds: 1, last_quote_age_seconds: 1, recent_quote_count: 0 },
        risk: { paused: false, kill_switch: false, pause_reason: '', daily_pnl: 0, consecutive_losses: 0 },
        symbol_runtimes: [],
      },
    }).as('getDiagnostics')

    cy.visit('/#/review')
    cy.get('input[placeholder="例如 AAPL.US"]').invoke('val', 'AAPL.US').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(1).find('input').first().invoke('val', '2026-06-01').trigger('input').trigger('change')
    cy.get('.el-form-item').eq(2).find('input').first().invoke('val', '2026-06-03').trigger('input').trigger('change')
    cy.contains('button', '查询').click()

    cy.wait('@getReview')
    cy.wait('@getStatusHistory')
    cy.wait('@getDiagnostics')

    cy.get('[data-testid="review-event-buckets"]').should('contain', '事件分桶（当前筛选结果）')
      .and('contain', '风险 2')
      .and('contain', '订单 3')
      .and('contain', '时段 1')
      .and('contain', '券商 2')
      .and('contain', 'LLM 2')
      .and('contain', '其他 1')
      .and('contain', '总计 11')

    cy.get('[data-testid="review-event-buckets"]').should('not.contain', 'BROKER_ERROR 1').and('not.contain', 'LLM_ERROR 1')

    cy.get('[data-testid="review-day-filter-event"]').click()
    cy.get('[data-testid="review-event-buckets"]').should('contain', '总计 11')

    cy.get('input[placeholder="搜索当前复盘：订单/事件/LLM/快照"]').clear().type('cooldown')
    cy.get('[data-testid="review-visible-day-count"]').should('contain', '1 / 3')
    cy.get('[data-testid="review-event-buckets"]').should('contain', '总计 3')

    cy.get('input[placeholder="搜索当前复盘：订单/事件/LLM/快照"]').clear().type('broker_retry')
    cy.get('[data-testid="review-visible-day-count"]').should('contain', '1 / 3')
    cy.get('[data-testid="review-event-buckets"]').should('contain', '总计 8')
  })
})
