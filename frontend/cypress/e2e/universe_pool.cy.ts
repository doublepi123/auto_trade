const refreshedRun = {
  id: 8,
  as_of_date: '2026-07-24',
  algorithm_version: 'index-liquidity-opportunity-v11',
  source_version: 'nasdaq-100_djia-v1',
  status: 'COMPLETE',
  candidate_count: 3,
  evaluable_count: 3,
  selected_count: 1,
  coverage_ratio: 1,
  parameters: { max_selected: 8 },
  error: '',
  started_at: '2026-07-24T02:00:00Z',
  completed_at: '2026-07-24T02:00:09Z',
  created_at: '2026-07-24T02:00:00Z',
  items: [
    {
      symbol: 'JPM.US',
      market: 'US',
      alias: 'JPMorgan Chase',
      sector: 'Financials',
      memberships: ['DJIA'],
      selected: true,
      exploration_selected: false,
      shadow_enabled: true,
      is_trading_target: false,
      rank: 1,
      score: 89.2,
      metrics: {
        price: 293.1,
        avg_dollar_volume: 2300000000,
        relative_spread_bps: 1.6,
        realized_vol_20d: 0.24,
        atr_pct_14d: 1.51,
        momentum_5d_pct: 2.2,
        trend_efficiency_10d: 0.2,
        opportunity_to_cost_ratio: 6.4,
        rotation: {
          algorithm_version: 'index-momentum-12-1-diversified-monthly-shadow-v3',
          lookback_bars: 252,
          skip_bars: 21,
          sma_bars: 200,
          momentum_pct: 28.9,
          sma_price: 248.7,
          above_sma: true,
          eligible: true,
          selected: true,
          rank: 1,
          score: 100,
          exclusion_reasons: [],
        },
      },
      exclusion_reasons: [],
      created_at: '2026-07-24T02:00:09Z',
    },
  ],
}

describe('Dynamic universe observation pool', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.intercept('POST', '/api/universe/refresh', {
      body: {
        run: refreshedRun,
        exploration_symbols: [],
        added_symbols: ['JPM.US'],
        removed_symbols: ['NVDA.US'],
        retained_symbols: [],
        shadow_enabled_symbols: [],
        shadow_disabled_symbols: [],
        shadow_failed_symbols: [],
        applied: false,
        reason: 'shadow observation only',
      },
    }).as('refreshUniverse')
    cy.visit('/#/watchlist')
    cy.wait('@getUniverseCatalog')
    cy.wait('@getUniverseLatest')
    cy.wait('@getUniversePromotionReadiness')
    cy.wait('@getRotationForwardScorecard')
  })

  it('shows provenance, coverage, ranking metrics and selection reasons', () => {
    cy.get('[data-testid="universe-panel"]').within(() => {
      cy.contains('动态候选池').should('be.visible')
      cy.contains('动态筛选').should('be.visible')
      cy.contains('只读观察').should('be.visible')
      cy.get('[data-testid="universe-as-of"]').should('contain', '2026-07-23')
      cy.get('[data-testid="universe-coverage"]').should('contain', '95.0%')
      cy.contains('当前实盘').should('be.visible')
      cy.contains('候选入选').should('be.visible')
      cy.contains('探索观察').should('be.visible')
      cy.contains('Shadow 已启用').should('be.visible')
      cy.contains('轮动影子').should('be.visible')
      cy.get('[data-testid="universe-rotation-count"]').should('contain', '2')
      cy.get('[data-testid="rotation-forward"]').within(() => {
        cy.contains('本月固定组合').should('be.visible')
        cy.contains('回填观察').should('be.visible')
        cy.get('[data-testid="rotation-forward-symbols"]')
          .should('contain', 'NVDA.US')
          .and('contain', 'AAPL.US')
        cy.get('[data-testid="rotation-forward-metrics"]')
          .should('contain', '+6.9%')
          .and('contain', '+3.7%')
          .and('contain', '+5.1%')
          .and('contain', '0.2%')
          .and('contain', '0/16')
        cy.contains('不计入前向晋级').should('be.visible')
        cy.contains('下月将在入场前冻结').should('be.visible')
      })
      cy.get('[data-testid="rotation-concentration-challenger"]').within(() => {
        cy.contains('集中 Top6 影子').should('be.visible')
        cy.contains('每风险组最多 2 只').should('be.visible')
        cy.get('[data-testid="rotation-concentration-symbols"]')
          .should('contain', 'NVDA.US')
          .and('contain', 'AAPL.US')
        cy.get('[data-testid="rotation-concentration-metrics"]')
          .should('contain', '+8.4%')
          .and('contain', '+1.5%')
          .and('contain', '+5.2%')
          .and('contain', '0.2%')
          .and('contain', '0/16')
        cy.contains('月末会与 Top8 同时预登记').should('be.visible')
      })
      cy.get('[data-testid="rotation-weighting-challenger"]').within(() => {
        cy.contains('波动配权影子').should('be.visible')
        cy.contains('25% 单票上限').should('be.visible')
        cy.get('[data-testid="rotation-weighting-symbols"]')
          .should('contain', 'NVDA.US 25.0%')
          .and('contain', 'AAPL.US 25.0%')
        cy.get('[data-testid="rotation-weighting-metrics"]')
          .should('contain', '+5.8%')
          .and('contain', '-1.1%')
          .and('contain', '+2.6%')
          .and('contain', '0.1%')
          .and('contain', '0/16')
        cy.contains('同时预登记下月权重').should('be.visible')
      })
      cy.get('[data-testid="rotation-shrinkage-challenger"]').within(() => {
        cy.contains('收缩配权影子').should('be.visible')
        cy.contains('75% 等权 + 25% 逆波动').should('be.visible')
        cy.contains('15% 上限').should('be.visible')
        cy.get('[data-testid="rotation-shrinkage-symbols"]')
          .should('contain', 'NVDA.US 15.0%')
          .and('contain', 'AAPL.US 15.0%')
        cy.get('[data-testid="rotation-shrinkage-metrics"]')
          .should('contain', '+6.4%')
          .and('contain', '-0.5%')
          .and('contain', '+3.2%')
          .and('contain', '0.2%')
          .and('contain', '0/16')
        cy.contains('与其他权重同时预登记').should('be.visible')
      })
      cy.get('[data-testid="rotation-return-to-variance-challenger"]').within(() => {
        cy.contains('收益/方差排名影子').should('be.visible')
        cy.contains('12-1 形成期').should('be.visible')
        cy.get('[data-testid="rotation-return-to-variance-symbols"]')
          .should('contain', 'JNJ.US 50.0%')
          .and('contain', 'GOOGL.US 50.0%')
        cy.get('[data-testid="rotation-return-to-variance-metrics"]')
          .should('contain', '+7.9%')
          .and('contain', '+1.0%')
          .and('contain', '+4.7%')
          .and('contain', '0.1%')
          .and('contain', '0/16')
        cy.contains('月末会按形成期收益/方差预登记下月组合').should('be.visible')
      })
      cy.get('[data-testid="rotation-forward-scorecard"]').within(() => {
        cy.contains('跨月前向记分牌').should('be.visible')
        cy.contains('完整月 3 期起').should('be.visible')
        cy.contains('Run #7').should('be.visible')
        cy.contains('19 次历史运行').should('be.visible')
        cy.get('[data-testid="rotation-forward-scorecard-table"] tbody tr')
          .should('have.length', 5)
          .first()
          .should('contain', '分散 Top8')
          .and('contain', '等待预登记')
          .and('contain', '0/3')
          .and('contain', '2026-07 回填诊断')
          .and('contain', '-2.0%')
          .and('contain', 'QQQ +4.1%')
          .and('contain', 'DIA -1.6%')
          .and('contain', '完整月样本不足')
          .and('contain', '回填月份已排除')
        cy.get('[data-testid="rotation-forward-scorecard-note"]')
          .should('contain', '回填诊断仅展示当前表现')
          .and('contain', '永不计入评分或晋级')
          .should('contain', '同时跑赢 QQQ 与 DIA')
          .and('contain', '只开放人工复核')
        cy.get('button').should('not.exist')
      })
      cy.get('[data-testid="rotation-walk-forward"]').within(() => {
        cy.contains('评估完成').should('be.visible')
        cy.get('[data-testid="rotation-training-winner"]').should('contain', '集中 Top6')
        cy.get('[data-testid="rotation-training-winner-gate"]')
          .should('contain', '未通过双门槛')
        cy.get('[data-testid="rotation-validated-challenger"]').should('contain', '波动配权 Top8')
        cy.get('[data-testid="rotation-validated-challenger-gate"]')
          .should('contain', '通过双门槛')
        cy.get('[data-testid="rotation-validation-metrics"]')
          .should('contain', '+46.2%')
          .and('contain', '+37.1%')
          .and('contain', '+11.5%')
          .and('contain', '1.78')
          .and('contain', '12.8%')
          .and('contain', '3/3')
        cy.contains('幸存者偏差').should('be.visible')
        cy.contains('不会自动晋级或下单').should('be.visible')
      })
      cy.get('[data-testid="rotation-point-in-time"]').within(() => {
        cy.contains('点时成分敏感性').should('be.visible')
        cy.contains('部分消偏').should('be.visible')
        cy.get('[data-testid="rotation-point-in-time-coverage"]')
          .should('contain', '169/171')
        cy.get('[data-testid="rotation-point-in-time-historical-coverage"]')
          .should('contain', '169/169')
        cy.get('[data-testid="rotation-point-in-time-metrics"]')
          .should('contain', '集中 Top6')
          .and('contain', '+57.4%')
          .and('contain', '1.92')
          .and('contain', '13.0%')
          .and('contain', '2/3')
          .and('contain', '收益/方差 Top8')
          .and('contain', '+32.9%')
          .and('contain', '1.57')
          .and('contain', '13.9%')
          .and('contain', '1/3')
          .and('contain', '等权 Top8')
          .and('contain', '+42.1%')
          .and('contain', '1.79')
          .and('contain', '12.7%')
          .and('contain', '3/3')
          .and('contain', '收缩配权 Top8')
          .and('contain', '+40.6%')
          .and('contain', '12.4%')
          .and('contain', '3/3')
          .and('contain', '波动配权 Top8')
          .and('contain', '+33.2%')
          .and('contain', '1.74')
          .and('contain', '11.3%')
          .and('contain', '2/3')
        cy.contains('仅作敏感性诊断').should('be.visible')
        cy.contains('不参与晋级或下单').should('be.visible')
        cy.contains('评估期行情缺失 2').should('be.visible')
        cy.contains('窗外行情缺失 2').should('be.visible')
        cy.contains('全局预热 253 个共同交易日').should('be.visible')
      })
      cy.contains('+46.8%').should('be.visible')
      cy.contains('$12.40B').should('be.visible')
      cy.contains('1.8 bp').should('be.visible')
      cy.contains('42.0%').should('be.visible')
      cy.contains('2.35%').should('be.visible')
      cy.contains('行业名额已满').should('be.visible')
    })
    cy.get('[data-testid="universe-table"] tbody tr')
      .contains('tr', 'NVDA.US')
      .should('contain', '当前实盘')
      .and('contain', '轮动影子')
      .and('contain', '#1')
    cy.get('[data-testid="universe-table"] tbody tr')
      .contains('tr', 'JPM.US')
      .should('not.contain', '当前实盘')
    cy.get('.universe-table-view').then(($view) => {
      const viewRect = $view[0].getBoundingClientRect()
      cy.get('[data-testid="universe-table"] tbody tr')
        .first()
        .find('td')
        .last()
        .then(($lastCell) => {
          const cellRect = $lastCell[0].getBoundingClientRect()
          expect(cellRect.right).to.be.at.most(viewRect.right + 1)
          expect(cellRect.left).to.be.at.least(viewRect.left - 1)
        })
    })
  })

  it('shows manual forward evidence and makes selection conflicts visible', () => {
    cy.get('[data-testid="promotion-readiness"]').within(() => {
      cy.contains('前瞻证据').should('be.visible')
      cy.contains('仅人工升级').should('be.visible')
      cy.contains('不自动切换').should('be.visible')
      cy.contains('Run #7').should('be.visible')
      cy.contains('融合优先').should('be.visible')
      cy.get('[data-testid="promotion-manual-note"]')
        .should('contain', '人工复核')
        .and('contain', '不提供自动升级或自动切换')
      cy.get('button').should('not.exist')
    })

    cy.get('[data-testid="promotion-readiness-table"] tbody tr')
      .contains('tr', 'NVDA.US')
      .should('contain', '当前实盘')
      .and('contain', '#1')
      .and('contain', '92.4')
      .and('contain', '24.0')
      .and('contain', '回避')
      .and('contain', '已过期')
      .and('contain', '前向采集中')
      .and('contain', '2/20')
      .and('contain', '+$18.70')
      .and('contain', '+$6.20')
      .and('contain', '候选 4')
      .and('contain', '基线 3')
      .and('contain', '当前代量化评分已过期')

    cy.get('[data-testid="promotion-readiness-table"] tbody tr')
      .contains('tr', 'JPM.US')
      .should('contain', '探索池')
      .and('contain', '数据异常')
      .and('contain', '-25.0')
      .and('not.contain', '0.0')
      .and('contain', '已阻塞')
      .and('contain', '基线与候选输入不一致')
      .and('contain', '量化评分数据异常')
  })

  it('isolates malformed forward evidence from the candidate pool', () => {
    cy.intercept('GET', '/api/universe/promotion-readiness', {
      body: {
        universe_run_id: 7,
        as_of_date: '2026-07-23',
        generated_at: '2026-07-24T01:05:00Z',
        priority_algorithm_version: 'selection-exploration-quant-core-satellite-observation-v7',
        diversified_observation_limit: 8,
        growth_satellite_limit: 4,
        items: {},
      },
    }).as('getUniversePromotionReadinessInvalid')

    cy.reload()
    cy.wait('@getUniversePromotionReadinessInvalid')
    cy.get('[data-testid="promotion-readiness-error"]')
      .should('contain', 'items is not an array')
    cy.get('[data-testid="universe-table"]')
      .should('be.visible')
      .and('contain', 'NVDA.US')
  })

  it('isolates a malformed rotation scorecard from the candidate pool', () => {
    cy.intercept('GET', '/api/universe/rotation-forward-scorecard', {
      body: {
        algorithm_version: 'rotation-forward-scorecard-v1',
        universe_run_id: 7,
        as_of_date: '2026-07-23',
        generated_at: '2026-07-24T01:05:00Z',
        source_run_count: 19,
        tracks: {},
        automatic_promotion_allowed: false,
      },
    }).as('getRotationForwardScorecardInvalid')

    cy.reload()
    cy.wait('@getRotationForwardScorecardInvalid')
    cy.get('[data-testid="rotation-forward-scorecard-error"]')
      .should('contain', 'tracks is invalid')
    cy.get('[data-testid="universe-table"]')
      .should('be.visible')
      .and('contain', 'NVDA.US')
  })

  it('uses the candidate API marker when the trading target is absent from watchlist', () => {
    cy.intercept('GET', '/api/watchlist', {
      body: [
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
    }).as('getWatchlistWithoutPrimary')
    cy.intercept('GET', '/api/universe/latest', {
      body: {
        ...refreshedRun,
        id: 9,
        items: [
          {
            ...refreshedRun.items[0],
            is_trading_target: true,
          },
        ],
      },
    }).as('getUniversePrimaryOutsideWatchlist')

    cy.reload()
    cy.wait('@getWatchlistWithoutPrimary')
    cy.wait('@getUniversePrimaryOutsideWatchlist')

    cy.get('[data-testid="universe-table"] tbody tr')
      .contains('tr', 'JPM.US')
      .should('contain', '当前实盘')
  })

  it('refreshes candidate selection without presenting it as a live switch', () => {
    cy.get('[data-testid="universe-refresh"]').click()
    cy.wait('@refreshUniverse')
    cy.get('[data-testid="universe-as-of"]').should('contain', '2026-07-24')
    cy.get('[data-testid="universe-coverage"]').should('contain', '100.0%')
    cy.get('[data-testid="universe-table"]').should('contain', 'JPM.US')
    cy.get('[data-testid="universe-table"] tbody tr')
      .contains('tr', 'JPM.US')
      .should('not.contain', '当前实盘')
    cy.contains(
      '.el-message',
      '候选池已刷新：正式入选 1 个，探索观察 0 个，覆盖率 100.0%',
    ).should('be.visible')
    cy.get('[data-testid="universe-panel"]').should('contain', '入选不等于切换实盘')
  })

  it('keeps the previous evidence visible and surfaces refresh failures', () => {
    cy.intercept('POST', '/api/universe/refresh', {
      statusCode: 503,
      body: { detail: '行情服务暂不可用' },
    }).as('refreshUniverseFailure')

    cy.get('[data-testid="universe-refresh"]').click()
    cy.wait('@refreshUniverseFailure')
    cy.get('[data-testid="universe-error"]').should('contain', '行情服务暂不可用')
    cy.get('[data-testid="universe-as-of"]').should('contain', '2026-07-23')
  })

  it('warns when selection succeeds but shadow synchronization is partial', () => {
    cy.intercept('POST', '/api/universe/refresh', {
      body: {
        run: refreshedRun,
        exploration_symbols: [],
        added_symbols: ['JPM.US'],
        removed_symbols: [],
        retained_symbols: [],
        shadow_enabled_symbols: [],
        shadow_disabled_symbols: [],
        shadow_failed_symbols: ['enable:JPM.US'],
        applied: true,
        reason: 'candidate watchlist reconciled; shadow sync failed for enable:JPM.US',
      },
    }).as('refreshUniversePartialShadow')

    cy.get('[data-testid="universe-refresh"]').click()
    cy.wait('@refreshUniversePartialShadow')
    cy.contains(
      '.el-message',
      '部分 Shadow 同步失败：enable:JPM.US',
    ).should('be.visible')
  })

  it('treats a missing latest run as an expected empty state', () => {
    cy.intercept('GET', '/api/universe/latest', {
      statusCode: 404,
      body: { detail: 'no universe selection run available' },
    }).as('getUniverseMissing')

    cy.reload()
    cy.wait('@getUniverseMissing')
    cy.get('[data-testid="universe-panel"]')
      .should('contain', '候选目录已加载 3 个标的，尚无筛选记录')
      .and('not.contain', 'no universe selection run available')
  })

  it('uses the compact candidate list without horizontal overflow on mobile', () => {
    cy.viewport(390, 844)
    cy.reload()
    cy.wait('@getUniverseCatalog')
    cy.wait('@getUniverseLatest')
    cy.wait('@getUniversePromotionReadiness')
    cy.get('[data-testid="desktop-nav"]').should('not.exist')
    cy.get('[data-testid="bottom-nav"]').should('be.visible')
    cy.get('[data-testid="universe-mobile-list"]').should('be.visible')
    cy.get('[data-testid="universe-mobile-list"]').should('contain', 'NVDA.US')
      .and('contain', '纳指 100')
      .and('contain', '道指')
    cy.get('[data-testid="promotion-readiness-mobile-list"]')
      .should('be.visible')
      .and('contain', 'NVDA.US')
      .and('contain', '融合 #1')
      .and('contain', '24.0 · 回避')
      .and('contain', '2/20')
      .and('contain', '当前代量化评分已过期')
    cy.get('body').then(($body) => {
      const viewportWidth = $body[0].clientWidth
      const overflowing = Array.from(
        $body[0].querySelectorAll<HTMLElement>('*'),
      )
        .map((element) => {
          const rect = element.getBoundingClientRect()
          return {
            tag: element.tagName,
            className: element.className,
            testId: element.dataset.testid,
            left: Math.round(rect.left),
            right: Math.round(rect.right),
            width: Math.round(rect.width),
          }
        })
        .filter((rect) => rect.right > viewportWidth + 1 && rect.width > 0)
        .slice(0, 24)
      expect(
        $body[0].scrollWidth,
        `overflowing elements: ${JSON.stringify(overflowing)}`,
      ).to.be.lte(viewportWidth)
    })
  })

  it('uses the compact evidence list across the app mobile breakpoint', () => {
    cy.viewport(750, 900)
    cy.reload()
    cy.wait('@getUniverseCatalog')
    cy.wait('@getUniverseLatest')
    cy.wait('@getUniversePromotionReadiness')
    cy.get('[data-testid="desktop-nav"]').should('not.exist')
    cy.get('[data-testid="promotion-readiness-table"]').should('not.be.visible')
    cy.get('[data-testid="promotion-readiness-mobile-list"]').should('be.visible')
    cy.get('body').then(($body) => {
      expect($body[0].scrollWidth).to.be.lte($body[0].clientWidth)
    })
  })
})
