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
  })

  it('renders live features, gate reasons, metrics and decisions', () => {
    openShadowTab()

    cy.get('[data-testid="shadow-latest-signal"]')
      .should('contain', '210.80 / -1.15')
      .and('contain', '211.10 / -0.42')
      .and('contain', '18.40')
      .and('contain', '等待 1m 价格重新收复')

    cy.get('[data-testid="shadow-config-section"]').should('contain', 'ARMED_LONG')

    cy.get('[data-testid="shadow-metrics"]')
      .should('contain', '34.20')
      .and('contain', '75.0%')
      .and('contain', '尚未建立版本一致的实盘对照基线')
      .and('contain', '21.5m')

    cy.get('[data-testid="shadow-evaluation"]')
      .should('contain', '采集中')
      .and('contain', '交易日 7 / 20')
      .and('contain', '闭环交易 4 / 50')

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
          algorithm_version: 'strategy-v2-rth-mr-v2-contiguous',
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
