describe('Strategy v2 shadow lab', () => {
  beforeEach(() => {
    cy.visitApp('/#/lab')
  })

  function openShadowTab() {
    cy.contains('.el-tabs__item', '策略 v2 影子').click()
    cy.wait([
      '@getStrategyShadowConfigs',
      '@getStrategyShadowConfig',
      '@getStrategyShadowVersions',
      '@getStrategyShadowStatus',
      '@getStrategyShadowDecisions',
      '@getStrategyShadowEvaluation',
      '@evaluateStrategyShadowAdxChallengers',
    ])
  }

  it('shows fixed dual-timeframe and hard no-order safety state', () => {
    openShadowTab()

    cy.get('[data-testid="shadow-safety-tags"]')
      .should('contain', '影子观察')
      .and('contain', '永不下单')
      .and('contain', '1m 触发')
      .and('contain', '5m 确认')
      .and('contain', '采集中')

    cy.get('[data-testid="shadow-symbol-select"]').should('contain', 'NVDA.US')

    cy.get('[data-testid="shadow-hard-safety"]')
      .should('contain', '60 分钟')
      .and('contain', '收盘前 45 分钟')
      .and('contain', '收盘前 15 分钟')
      .and('contain', '加仓')
      .and('contain', '做空')
      .and('contain', '订单提交')
      .and('contain', '禁止')

    cy.get('[data-testid="tab-strategy-shadow"]').should('not.contain', '实盘应用')
    cy.get('[data-testid="shadow-adx-challengers"]').should('not.contain', '应用参数')
    cy.get('[data-testid="shadow-warmup-diagnostic"]').should('not.contain', '应用参数')
  })

  it('renders live features, gate reasons, metrics and decisions', () => {
    openShadowTab()

    cy.get('[data-testid="shadow-latest-signal"]')
      .should('contain', '210.80 / -1.15')
      .and('contain', '211.10 / -0.42')
      .and('contain', '18.40')
      .and('contain', '等待 1m 价格重新收复')

    cy.get('[data-testid="shadow-config-section"]').should('contain', 'ARMED_LONG')
    cy.get('[data-testid="shadow-version-transition"]').should('not.exist')

    cy.get('[data-testid="shadow-metrics"]')
      .should('contain', '34.20')
      .and('contain', '75.0%')
      .and('contain', '尚未建立版本一致的实盘对照基线')
      .and('contain', '21.5m')

    cy.get('[data-testid="shadow-evaluation"]')
      .should('contain', '采集中')
      .and('contain', '交易日 7 / 20')
      .and('contain', '闭环交易 4 / 50')
    cy.get('[data-testid="shadow-evidence-excluded"]')
      .should('contain', '排除交易日 1')
      .and('contain', '排除闭环交易 2')
    cy.get('[data-testid="shadow-evidence-blockers"]')
      .should('contain', '完整交易日不足')
      .and('contain', '完整会话闭环交易不足')
      .and('contain', '费用压力后净收益不为正')
    cy.get('[data-testid="shadow-evidence-warnings"]')
      .should('contain', '1 internal bars missing')
    cy.get('[data-testid="shadow-evidence-daily"]')
      .should('contain', '11:49')
      .and('contain', '251 / 139')
    cy.get('[data-testid="shadow-evidence-daily"] .el-table__expand-icon').first().click()
    cy.get('[data-testid="shadow-evidence-daily"]')
      .should('contain', '09:00-09:59')
      .and('contain', 'ADX_5M_WARMUP 60')

    cy.get('[data-testid="shadow-warmup-diagnostic"]')
      .should('contain', '预热与分时可用性')
      .and('contain', '因果配对 1 / 5')
      .and('contain', '日内冷启动')
      .and('contain', '因果趋势预热')
      .and('contain', '139')
      .and('contain', '64')
      .and('contain', '+75')
      .and('contain', '+15')
      .and('not.contain', '应用参数')
    cy.get('[data-testid="shadow-warmup-readonly"]')
      .should('contain', '仅预热 ADX / 波动率')
      .and('contain', 'VWAP 与 z-score 仍按交易日重置')
      .and('contain', '不会写入状态或提交订单')
    cy.get('[data-testid="shadow-warmup-insufficient"]')
      .should('contain', '因果配对交易日不足')
      .and('contain', '至少需要 5 对')
    cy.get('[data-testid="shadow-warmup-hourly"]')
      .should('contain', '09:00-09:59')
      .and('contain', '10:00-10:59')
      .and('contain', '0 / 56')
      .and('contain', '+56')
    cy.get('[data-testid="shadow-warmup-variants"] .el-table__expand-icon').first().click()
    cy.get('[data-testid="shadow-warmup-variants"]').should('contain', '11:49')
    cy.get('[data-testid="shadow-warmup-variants"] .el-table__expand-icon').eq(1).click()
    cy.get('[data-testid="shadow-warmup-variants"]')
      .should('contain', '10:34')
      .and('contain', '2026-07-09')

    cy.get('[data-testid="shadow-adx-challengers"]')
      .should('contain', 'ADX 同样本对照')
      .and('contain', '即时回放不落库，永不提交订单')
      .and('contain', '完整交易日 3 / 5')
      .and('contain', '方案 3')
      .and('contain', '基线')
      .and('contain', '挑战者')
      .and('contain', '20.0')
      .and('contain', '25.0')
      .and('contain', '30.0')
      .and('contain', '41.60')
    cy.get('[data-testid="shadow-adx-replay"]').should('contain', '基线复放一致')
    cy.get('[data-testid="shadow-adx-exploratory"]')
      .should('contain', '样本内探索：不可晋级')
      .and('contain', '需后续前向验证')
    cy.get('[data-testid="shadow-adx-insufficient"]')
      .should('contain', '完整同样本交易日不足')
      .and('contain', '至少需要 5 日')

    cy.get('[data-testid="shadow-gates"]')
      .should('contain', 'WAIT_BREACH')
      .and('contain', 'ADX')
      .and('contain', 'VOL_HIGH')

    cy.get('[data-testid="shadow-decisions"]')
      .should('contain', 'WAIT_RECLAIM')
      .and('contain', '等待收复')

    cy.get('[data-testid="shadow-export-decisions"]').click()
    cy.document().its('body').should('contain', '已导出 1 条影子决策')
  })

  it('distinguishes a frozen evidence version while a virtual trade exits', () => {
    cy.intercept('GET', '/api/strategy-shadow/status*', (req) => {
      req.on('before:response', (response) => {
        response.body.config.config_version = 'shadow-new-v4'
        response.body.evidence_config_version = 'shadow-old-v0'
        response.body.version_transition_pending = true
        response.body.latest.virtual_position = 'LONG'
      })
    })

    openShadowTab()

    cy.get('[data-testid="shadow-version-transition"]')
      .should('contain', '版本切换等待中')
      .and('contain', '旧虚拟仓位')
      .and('contain', '配置 shadow-o')
      .and('contain', '配置 shadow-n')
      .and('contain', '平仓后切换')
    cy.get('[data-testid="shadow-config-section"]')
      .should('contain', '当前 配置 shadow-n')
    cy.get('[data-testid="shadow-latest-signal"]')
      .should('contain', '证据 配置 shadow-o')
    cy.get('[data-testid="shadow-metrics"]')
      .should('contain', '证据 配置 shadow-o')
    cy.get('[data-testid="shadow-gates"]')
      .should('contain', '证据 配置 shadow-o')
  })

  it('reports a flat version initialization without claiming a virtual exit', () => {
    cy.intercept('GET', '/api/strategy-shadow/status*', (req) => {
      req.on('before:response', (response) => {
        response.body.config.enabled = false
        response.body.evidence_config_version = response.body.config.config_version
        response.body.version_transition_pending = true
        response.body.phase = 'DISABLED'
      })
    })

    openShadowTab()

    cy.get('[data-testid="shadow-version-transition"]')
      .should('contain', '运行状态尚待初始化')
      .and('contain', '采集仍停用')
      .and('not.contain', '旧虚拟仓位')
    cy.get('[data-testid="shadow-config-section"]')
      .should('contain', 'DISABLED')
      .and('contain', '当前 配置 shadow-s')
  })

  it('shows a hard blocker when baseline replay does not match', () => {
    cy.intercept('POST', '/api/strategy-shadow/adx-challengers', {
      body: {
        persisted: false,
        mode: 'SHADOW',
        order_submission_allowed: false,
        evaluation_scope: 'EXPLORATORY_IN_SAMPLE',
        promotion_eligible: false,
        forward_validation_required: true,
        symbol: 'NVDA.US',
        source_config_version: 'shadow-stub-v1',
        status: 'BLOCKED',
        minimum_complete_sessions: 5,
        observed_complete_sessions: 1,
        evaluated_complete_sessions: 1,
        baseline_replay_match: false,
        blockers: ['MIN_COMPLETE_SESSIONS', 'BASELINE_REPLAY_MISMATCH'],
        warmup_diagnostic: {
          algorithm_version: 'strategy-v2-causal-trend-prewarm-v1',
          status: 'BLOCKED',
          minimum_causal_pairs: 5,
          observed_causal_pairs: 0,
          evaluated_causal_pairs: 0,
          blockers: ['BASELINE_REPLAY_MISMATCH'],
          same_sample: true,
          causal_history_only: true,
          vwap_zscore_session_local: true,
          variants: [],
        },
        candidates: [{
          label: 'BASELINE',
          max_adx: 25,
          config_version: 'shadow-stub-v1',
          metrics: {
            bars: 390,
            eligible_bars: 25,
            breaches: 3,
            reclaims: 2,
            entries: 2,
            exits: 2,
            closed_trades: 2,
            win_rate: 1,
            gross_pnl: 36.3,
            fees: 2.1,
            net_pnl: 34.2,
            max_drawdown: 0,
            avg_holding_minutes: 21.5,
            avg_mae_pct: 0.0032,
            avg_mfe_pct: 0.0078,
            comparison_available: false,
            live_action_count: null,
            action_agreement_rate: null,
            net_pnl_delta_vs_live: null,
          },
          daily: [{
            session_date: '2026-07-10',
            bars: 390,
            eligible_bars: 25,
            breaches: 3,
            reclaims: 2,
            closed_trades: 2,
            net_pnl: 34.2,
            max_drawdown: 0,
            exit_reasons: { PROFIT_TARGET: 2 },
          }],
        }],
      },
    }).as('evaluateStrategyShadowAdxChallengers')

    openShadowTab()

    cy.get('[data-testid="shadow-adx-replay"]').should('contain', '基线复放不一致')
    cy.get('[data-testid="shadow-adx-blocked"]')
      .should('contain', 'ADX 对照已阻塞')
      .and('contain', '基线回放与持久化指标不一致')
    cy.get('[data-testid="shadow-adx-candidates"]')
      .should('contain', '基线')
      .and('not.contain', '挑战者')
    cy.get('[data-testid="shadow-warmup-diagnostic"]').should('exist')
    cy.get('[data-testid="shadow-warmup-blocked"]')
      .should('contain', '因果预热诊断已阻塞')
      .and('contain', '基线回放与持久化证据不一致')
  })

  it('replays challengers only on full load, version change, and manual refresh', () => {
    openShadowTab()

    cy.get('[data-testid="shadow-version-select"]').click()
    cy.get('.el-select-dropdown:visible .el-select-dropdown__item')
      .contains('shadow-o')
      .click()
    cy.wait([
      '@getStrategyShadowEvaluation',
      '@getStrategyShadowDecisions',
      '@evaluateStrategyShadowAdxChallengers',
    ]).then((requests) => requests[2]?.request.body.config_version)
      .should('equal', 'shadow-old-v0')

    cy.get('[data-testid="shadow-refresh"]').click()
    cy.wait([
      '@getStrategyShadowConfigs',
      '@getStrategyShadowConfig',
      '@getStrategyShadowVersions',
      '@getStrategyShadowStatus',
      '@getStrategyShadowDecisions',
      '@getStrategyShadowEvaluation',
      '@evaluateStrategyShadowAdxChallengers',
    ]).then((requests) => requests[6]?.request.body.config_version)
      .should('equal', 'shadow-stub-v1')

    cy.get('@evaluateStrategyShadowAdxChallengers.all').should('have.length', 3)
    cy.wait(15_100)
    cy.wait(['@getStrategyShadowStatus', '@getStrategyShadowEvaluation'])
    cy.get('@evaluateStrategyShadowAdxChallengers.all').should('have.length', 3)
  })

  it('keeps core evidence and clears stale challenger data when replay fails', () => {
    openShadowTab()
    cy.get('[data-testid="shadow-adx-challengers"]').should('contain', '配置 shadow-s')

    cy.intercept('POST', '/api/strategy-shadow/adx-challengers', {
      statusCode: 504,
      body: { detail: 'ADX 回放超时' },
    }).as('failedStrategyShadowAdxChallengers')

    cy.get('[data-testid="shadow-version-select"]').click()
    cy.get('.el-select-dropdown:visible .el-select-dropdown__item')
      .contains('shadow-o')
      .click()
    cy.wait([
      '@getStrategyShadowEvaluation',
      '@getStrategyShadowDecisions',
      '@failedStrategyShadowAdxChallengers',
    ])

    cy.get('[data-testid="shadow-evaluation"]').should('be.visible')
    cy.get('[data-testid="shadow-load-error"]').should('not.exist')
    cy.get('[data-testid="shadow-adx-challengers"]').should('not.exist')
    cy.get('[data-testid="shadow-adx-error"]').should('contain', 'ADX 回放超时')
  })

  it('clears old core evidence when a version switch fails', () => {
    openShadowTab()
    cy.get('[data-testid="shadow-evaluation"]').should('contain', '配置 shadow-s')
    cy.get('[data-testid="shadow-decisions"]').should('contain', 'WAIT_RECLAIM')

    cy.intercept('GET', '/api/strategy-shadow/evaluation*', {
      statusCode: 500,
      body: { detail: '版本证据读取失败' },
    }).as('failedStrategyShadowEvaluation')

    cy.get('[data-testid="shadow-version-select"]').click()
    cy.get('.el-select-dropdown:visible .el-select-dropdown__item')
      .contains('shadow-o')
      .click()
    cy.wait('@failedStrategyShadowEvaluation')

    cy.get('[data-testid="shadow-evaluation"]').should('not.exist')
    cy.get('[data-testid="shadow-decisions"]').should('not.contain', 'WAIT_RECLAIM')
    cy.get('.el-message--error').should('contain', '版本证据读取失败')
  })

  it('keeps warmup diagnostics inside the mobile viewport', () => {
    cy.viewport(390, 844)
    openShadowTab()

    cy.get('[data-testid="shadow-warmup-diagnostic"]').should('be.visible')
    cy.document().then((document) => {
      expect(document.documentElement.scrollWidth).to.be.at.most(
        document.documentElement.clientWidth,
      )
    })
  })

  it('saves only shadow tunables and never sends execution safety fields', () => {
    cy.intercept({ method: 'PUT', pathname: '/api/strategy-shadow/config' }, (req) => {
      expect(req.body).to.have.property('enabled', false)
      expect(req.body).to.have.property('breach_zscore', -2)
      expect(req.body).to.have.property('reclaim_zscore', -1)
      expect(req.body).not.to.have.property('order_submission_allowed')
      expect(req.body).not.to.have.property('max_holding_minutes')
      expect(req.body).not.to.have.property('entry_cutoff_minutes_before_close')
      expect(req.body).not.to.have.property('flatten_minutes_before_close')
      expect(req.body).not.to.have.property('allow_position_addons')
      expect(req.body).not.to.have.property('short_entries_enabled')
      req.reply({
        body: {
          enabled: false,
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
          config_version: 'shadow-test-v2',
          updated_at: '2026-07-12T02:10:00Z',
        },
      })
    }).as('assertShadowSave')

    openShadowTab()
    cy.get('[data-testid="shadow-enabled"]').click()
    cy.get('[data-testid="shadow-save-config"]').click()
    cy.get('.el-message-box').should('be.visible')
    cy.get('.el-message-box__btns .el-button--primary').click()
    cy.wait('@assertShadowSave')
  })
})
