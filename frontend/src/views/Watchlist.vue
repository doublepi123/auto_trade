<template>
  <div class="watchlist-page">
    <div class="page-heading">
      <h3>观察列表</h3>
      <p>多标的行情观察（仅观察，不自动下单）</p>
    </div>

    <el-card class="universe-panel" data-testid="universe-panel">
      <template #header>
        <div class="universe-header">
          <div class="universe-heading">
            <div class="universe-title">
              <strong>动态候选池</strong>
              <el-tag type="info" size="small" effect="plain">动态筛选</el-tag>
              <el-tag type="warning" size="small" effect="plain">只读观察</el-tag>
            </div>
            <p>每日先筛正式候选，再补充跨行业探索与 12-1 中期轮动证据；入选不等于切换实盘，三层都不会自动下单。</p>
          </div>
          <el-button
            type="primary"
            plain
            :icon="Refresh"
            :loading="universeRefreshing"
            data-testid="universe-refresh"
            @click="handleUniverseRefresh"
          >
            刷新候选池
          </el-button>
        </div>
      </template>

      <el-alert
        v-if="universeError"
        :title="universeError"
        type="error"
        :closable="false"
        show-icon
        class="universe-alert"
        data-testid="universe-error"
      />

      <div v-loading="universeLoading && !universeRun" class="universe-content">
        <template v-if="universeRun">
          <div class="universe-summary" data-testid="universe-summary">
            <div class="universe-summary-item">
              <span>数据日期</span>
              <strong data-testid="universe-as-of">{{ universeRun.as_of_date }}</strong>
            </div>
            <div class="universe-summary-item">
              <span>运行状态</span>
              <el-tag :type="universeStatusTagType(universeRun.status)" size="small">
                {{ universeStatusLabel(universeRun.status) }}
              </el-tag>
            </div>
            <div class="universe-summary-item">
              <span>数据覆盖</span>
              <strong data-testid="universe-coverage">{{ formatCoverage(universeRun.coverage_ratio) }}</strong>
            </div>
            <div class="universe-summary-item">
              <span>候选入选</span>
              <strong>{{ universeRun.selected_count }}/{{ universeRun.evaluable_count }}</strong>
            </div>
            <div class="universe-summary-item">
              <span>探索观察</span>
              <strong>{{ universeExplorationCount }}</strong>
            </div>
            <div class="universe-summary-item">
              <span>中期轮动</span>
              <strong data-testid="universe-rotation-count">{{ universeRotationCount }}</strong>
            </div>
            <div class="universe-summary-item">
              <span>候选目录</span>
              <strong>{{ universeCatalog.length || universeRun.candidate_count }}</strong>
            </div>
            <div class="universe-summary-item universe-version">
              <span>算法版本</span>
              <strong :title="universeRun.algorithm_version">{{ universeRun.algorithm_version }}</strong>
            </div>
          </div>

          <el-alert
            v-if="universeRun.error"
            :title="universeRun.error"
            type="warning"
            :closable="false"
            show-icon
            class="universe-alert"
          />

          <section
            v-if="universeRotationForward"
            class="rotation-forward"
            data-testid="rotation-forward"
          >
            <div class="rotation-forward-header">
              <div>
                <div class="rotation-forward-title">
                  <strong>本月固定组合</strong>
                  <el-tag
                    :type="rotationForwardTagType(universeRotationForward.evidence_mode)"
                    size="small"
                    effect="plain"
                  >
                    {{ rotationForwardModeLabel(universeRotationForward.evidence_mode) }}
                  </el-tag>
                  <el-tag type="info" size="small" effect="plain">等权影子</el-tag>
                </div>
                <small>{{ universeRotationForward.algorithm_version }}</small>
              </div>
              <div class="rotation-forward-dates">
                <span>信号<strong>{{ universeRotationForward.signal_date || '-' }}</strong></span>
                <span>入场<strong>{{ universeRotationForward.entry_date || '-' }}</strong></span>
                <span>估值<strong>{{ universeRotationForward.mark_date || '-' }}</strong></span>
              </div>
            </div>

            <div class="rotation-forward-symbols" data-testid="rotation-forward-symbols">
              <span>固定成分</span>
              <div v-if="universeRotationForward.target_symbols.length">
                <el-tag
                  v-for="symbol in universeRotationForward.target_symbols"
                  :key="symbol"
                  size="small"
                  effect="plain"
                >
                  {{ symbol }}
                </el-tag>
              </div>
              <strong v-else>现金观察</strong>
            </div>

            <div class="rotation-forward-metrics" data-testid="rotation-forward-metrics">
              <div>
                <span>净清算收益</span>
                <strong>{{ formatSignedPercent(universeRotationForward.net_liquidation_return_pct) }}</strong>
              </div>
              <div>
                <span>超额 QQQ</span>
                <strong>{{ formatSignedPercent(universeRotationForward.excess_return_vs_qqq_pct) }}</strong>
              </div>
              <div>
                <span>超额 DIA</span>
                <strong>{{ formatSignedPercent(universeRotationForward.excess_return_vs_dia_pct) }}</strong>
              </div>
              <div>
                <span>完整估算成本</span>
                <strong>{{ formatPercent(universeRotationForward.total_estimated_cost_pct) }}</strong>
              </div>
              <div>
                <span>前向会话</span>
                <strong>{{ universeRotationForward.forward_observation_sessions }}/{{ universeRotationForward.elapsed_sessions }}</strong>
              </div>
            </div>

            <div class="rotation-forward-footer">
              <span>
                登记 {{ universeRotationForward.registered_as_of_date || '-' }}
                · {{ rotationVariantLabel(universeRotationForward.variant_name) }}
                · 只读观察
              </span>
              <strong v-if="universeRotationForward.evidence_mode === 'BACKFILLED_AFTER_ENTRY'">
                本月组合在月初后登记，不计入前向晋级；下月将在入场前冻结。
              </strong>
              <strong v-else-if="universeRotationForward.evidence_mode === 'FORWARD_PRECOMMITTED'">
                已在入场前冻结；本月会话计入前向证据，但仍不会自动晋级或下单。
              </strong>
              <strong v-else>
                月度证据暂不可用；轮动选择已关闭，不会回退为每日重排。
              </strong>
            </div>
          </section>

          <section
            v-if="universeRotationConcentrationChallenger"
            class="rotation-forward rotation-concentration-challenger"
            data-testid="rotation-concentration-challenger"
          >
            <div class="rotation-forward-header">
              <div>
                <div class="rotation-forward-title">
                  <strong>集中 Top6 影子</strong>
                  <el-tag
                    :type="rotationForwardTagType(universeRotationConcentrationChallenger.evidence_mode)"
                    size="small"
                    effect="plain"
                  >
                    {{ rotationForwardModeLabel(universeRotationConcentrationChallenger.evidence_mode) }}
                  </el-tag>
                  <el-tag type="warning" size="small" effect="plain">每风险组最多 2 只</el-tag>
                </div>
                <small>{{ universeRotationConcentrationChallenger.algorithm_version }}</small>
              </div>
              <div class="rotation-forward-dates">
                <span>
                  信号
                  <strong>{{ universeRotationConcentrationChallenger.signal_date || '-' }}</strong>
                </span>
                <span>
                  入场
                  <strong>{{ universeRotationConcentrationChallenger.entry_date || '-' }}</strong>
                </span>
                <span>
                  估值
                  <strong>{{ universeRotationConcentrationChallenger.mark_date || '-' }}</strong>
                </span>
              </div>
            </div>

            <div
              class="rotation-forward-symbols"
              data-testid="rotation-concentration-symbols"
            >
              <span>冻结成分</span>
              <div v-if="universeRotationConcentrationChallenger.holdings.length">
                <el-tag
                  v-for="holding in universeRotationConcentrationChallenger.holdings"
                  :key="holding.symbol"
                  size="small"
                  effect="plain"
                >
                  {{ holding.symbol }} {{ formatPercent(holding.weight_pct) }}
                </el-tag>
              </div>
              <strong v-else>现金观察</strong>
            </div>

            <div
              class="rotation-forward-metrics"
              data-testid="rotation-concentration-metrics"
            >
              <div>
                <span>净清算收益</span>
                <strong>{{ formatSignedPercent(universeRotationConcentrationChallenger.net_liquidation_return_pct) }}</strong>
              </div>
              <div>
                <span>相对 Top8</span>
                <strong>{{ formatSignedPercent(rotationConcentrationReturnDelta) }}</strong>
              </div>
              <div>
                <span>超额 QQQ</span>
                <strong>{{ formatSignedPercent(universeRotationConcentrationChallenger.excess_return_vs_qqq_pct) }}</strong>
              </div>
              <div>
                <span>完整估算成本</span>
                <strong>{{ formatPercent(universeRotationConcentrationChallenger.total_estimated_cost_pct) }}</strong>
              </div>
              <div>
                <span>前向会话</span>
                <strong>
                  {{ universeRotationConcentrationChallenger.forward_observation_sessions }}/{{ universeRotationConcentrationChallenger.elapsed_sessions }}
                </strong>
              </div>
            </div>

            <div class="rotation-forward-footer">
              <span>
                登记 {{ universeRotationConcentrationChallenger.registered_as_of_date || '-' }}
                · {{ rotationVariantLabel(universeRotationConcentrationChallenger.variant_name) }}
                · 并行只读
              </span>
              <strong v-if="universeRotationConcentrationChallenger.evidence_mode === 'BACKFILLED_AFTER_ENTRY'">
                当前仅作回填对照；月末会与 Top8 同时预登记下月组合。
              </strong>
              <strong v-else-if="universeRotationConcentrationChallenger.evidence_mode === 'FORWARD_PRECOMMITTED'">
                已在入场前冻结；只验证集中度溢价，不改变当前选股或触发订单。
              </strong>
              <strong v-else>
                集中度证据暂不可用，不会回退到事后重算。
              </strong>
            </div>
          </section>

          <section
            v-if="universeRotationWeightingChallenger"
            class="rotation-forward rotation-weighting-challenger"
            data-testid="rotation-weighting-challenger"
          >
            <div class="rotation-forward-header">
              <div>
                <div class="rotation-forward-title">
                  <strong>波动配权影子</strong>
                  <el-tag
                    :type="rotationForwardTagType(universeRotationWeightingChallenger.evidence_mode)"
                    size="small"
                    effect="plain"
                  >
                    {{ rotationForwardModeLabel(universeRotationWeightingChallenger.evidence_mode) }}
                  </el-tag>
                  <el-tag type="info" size="small" effect="plain">25% 单票上限</el-tag>
                </div>
                <small>{{ universeRotationWeightingChallenger.algorithm_version }}</small>
              </div>
              <div class="rotation-forward-dates">
                <span>
                  信号
                  <strong>{{ universeRotationWeightingChallenger.signal_date || '-' }}</strong>
                </span>
                <span>
                  入场
                  <strong>{{ universeRotationWeightingChallenger.entry_date || '-' }}</strong>
                </span>
                <span>
                  估值
                  <strong>{{ universeRotationWeightingChallenger.mark_date || '-' }}</strong>
                </span>
              </div>
            </div>

            <div
              class="rotation-forward-symbols"
              data-testid="rotation-weighting-symbols"
            >
              <span>冻结权重</span>
              <div v-if="universeRotationWeightingChallenger.holdings.length">
                <el-tag
                  v-for="holding in universeRotationWeightingChallenger.holdings"
                  :key="holding.symbol"
                  size="small"
                  effect="plain"
                >
                  {{ holding.symbol }} {{ formatPercent(holding.weight_pct) }}
                </el-tag>
              </div>
              <strong v-else>现金观察</strong>
            </div>

            <div
              class="rotation-forward-metrics"
              data-testid="rotation-weighting-metrics"
            >
              <div>
                <span>净清算收益</span>
                <strong>{{ formatSignedPercent(universeRotationWeightingChallenger.net_liquidation_return_pct) }}</strong>
              </div>
              <div>
                <span>相对等权</span>
                <strong>{{ formatSignedPercent(rotationWeightingReturnDelta) }}</strong>
              </div>
              <div>
                <span>超额 QQQ</span>
                <strong>{{ formatSignedPercent(universeRotationWeightingChallenger.excess_return_vs_qqq_pct) }}</strong>
              </div>
              <div>
                <span>完整估算成本</span>
                <strong>{{ formatPercent(universeRotationWeightingChallenger.total_estimated_cost_pct) }}</strong>
              </div>
              <div>
                <span>前向会话</span>
                <strong>
                  {{ universeRotationWeightingChallenger.forward_observation_sessions }}/{{ universeRotationWeightingChallenger.elapsed_sessions }}
                </strong>
              </div>
            </div>

            <div class="rotation-forward-footer">
              <span>
                登记 {{ universeRotationWeightingChallenger.registered_as_of_date || '-' }}
                · {{ rotationVariantLabel(universeRotationWeightingChallenger.variant_name) }}
                · 并行只读
              </span>
              <strong v-if="universeRotationWeightingChallenger.evidence_mode === 'BACKFILLED_AFTER_ENTRY'">
                当前仅作回填对照；月末会与等权组合同时预登记下月权重。
              </strong>
              <strong v-else-if="universeRotationWeightingChallenger.evidence_mode === 'FORWARD_PRECOMMITTED'">
                已在入场前冻结；只比较配权效果，不改变选股或触发订单。
              </strong>
              <strong v-else>
                配权证据暂不可用，不会回退到事后重算。
              </strong>
            </div>
          </section>

          <section
            v-if="universeRotationShrinkageChallenger"
            class="rotation-forward rotation-shrinkage-challenger"
            data-testid="rotation-shrinkage-challenger"
          >
            <div class="rotation-forward-header">
              <div>
                <div class="rotation-forward-title">
                  <strong>收缩配权影子</strong>
                  <el-tag
                    :type="rotationForwardTagType(universeRotationShrinkageChallenger.evidence_mode)"
                    size="small"
                    effect="plain"
                  >
                    {{ rotationForwardModeLabel(universeRotationShrinkageChallenger.evidence_mode) }}
                  </el-tag>
                  <el-tag type="info" size="small" effect="plain">75% 等权 + 25% 逆波动</el-tag>
                  <el-tag type="info" size="small" effect="plain">15% 上限</el-tag>
                </div>
                <small>{{ universeRotationShrinkageChallenger.algorithm_version }}</small>
              </div>
              <div class="rotation-forward-dates">
                <span>
                  信号
                  <strong>{{ universeRotationShrinkageChallenger.signal_date || '-' }}</strong>
                </span>
                <span>
                  入场
                  <strong>{{ universeRotationShrinkageChallenger.entry_date || '-' }}</strong>
                </span>
                <span>
                  估值
                  <strong>{{ universeRotationShrinkageChallenger.mark_date || '-' }}</strong>
                </span>
              </div>
            </div>

            <div
              class="rotation-forward-symbols"
              data-testid="rotation-shrinkage-symbols"
            >
              <span>冻结权重</span>
              <div v-if="universeRotationShrinkageChallenger.holdings.length">
                <el-tag
                  v-for="holding in universeRotationShrinkageChallenger.holdings"
                  :key="holding.symbol"
                  size="small"
                  effect="plain"
                >
                  {{ holding.symbol }} {{ formatPercent(holding.weight_pct) }}
                </el-tag>
              </div>
              <strong v-else>现金观察</strong>
            </div>

            <div
              class="rotation-forward-metrics"
              data-testid="rotation-shrinkage-metrics"
            >
              <div>
                <span>净清算收益</span>
                <strong>{{ formatSignedPercent(universeRotationShrinkageChallenger.net_liquidation_return_pct) }}</strong>
              </div>
              <div>
                <span>相对等权</span>
                <strong>{{ formatSignedPercent(rotationShrinkageReturnDelta) }}</strong>
              </div>
              <div>
                <span>超额 QQQ</span>
                <strong>{{ formatSignedPercent(universeRotationShrinkageChallenger.excess_return_vs_qqq_pct) }}</strong>
              </div>
              <div>
                <span>完整估算成本</span>
                <strong>{{ formatPercent(universeRotationShrinkageChallenger.total_estimated_cost_pct) }}</strong>
              </div>
              <div>
                <span>前向会话</span>
                <strong>
                  {{ universeRotationShrinkageChallenger.forward_observation_sessions }}/{{ universeRotationShrinkageChallenger.elapsed_sessions }}
                </strong>
              </div>
            </div>

            <div class="rotation-forward-footer">
              <span>
                登记 {{ universeRotationShrinkageChallenger.registered_as_of_date || '-' }}
                · {{ rotationVariantLabel(universeRotationShrinkageChallenger.variant_name) }}
                · 并行只读
              </span>
              <strong v-if="universeRotationShrinkageChallenger.evidence_mode === 'BACKFILLED_AFTER_ENTRY'">
                当前仅作回填对照；月末会与其他权重同时预登记下月组合。
              </strong>
              <strong v-else-if="universeRotationShrinkageChallenger.evidence_mode === 'FORWARD_PRECOMMITTED'">
                已在入场前冻结；只验证收缩配权，不改变选股或触发订单。
              </strong>
              <strong v-else>
                收缩配权证据暂不可用，不会回退到事后重算。
              </strong>
            </div>
          </section>

          <section
            v-if="universeRotationReturnToVarianceChallenger"
            class="rotation-forward rotation-return-to-variance-challenger"
            data-testid="rotation-return-to-variance-challenger"
          >
            <div class="rotation-forward-header">
              <div>
                <div class="rotation-forward-title">
                  <strong>收益/方差排名影子</strong>
                  <el-tag
                    :type="rotationForwardTagType(universeRotationReturnToVarianceChallenger.evidence_mode)"
                    size="small"
                    effect="plain"
                  >
                    {{ rotationForwardModeLabel(universeRotationReturnToVarianceChallenger.evidence_mode) }}
                  </el-tag>
                  <el-tag type="info" size="small" effect="plain">12-1 形成期</el-tag>
                  <el-tag type="info" size="small" effect="plain">收益 / 方差</el-tag>
                </div>
                <small>{{ universeRotationReturnToVarianceChallenger.algorithm_version }}</small>
              </div>
              <div class="rotation-forward-dates">
                <span>
                  信号
                  <strong>{{ universeRotationReturnToVarianceChallenger.signal_date || '-' }}</strong>
                </span>
                <span>
                  入场
                  <strong>{{ universeRotationReturnToVarianceChallenger.entry_date || '-' }}</strong>
                </span>
                <span>
                  估值
                  <strong>{{ universeRotationReturnToVarianceChallenger.mark_date || '-' }}</strong>
                </span>
              </div>
            </div>

            <div
              class="rotation-forward-symbols"
              data-testid="rotation-return-to-variance-symbols"
            >
              <span>冻结成分</span>
              <div v-if="universeRotationReturnToVarianceChallenger.holdings.length">
                <el-tag
                  v-for="holding in universeRotationReturnToVarianceChallenger.holdings"
                  :key="holding.symbol"
                  size="small"
                  effect="plain"
                >
                  {{ holding.symbol }} {{ formatPercent(holding.weight_pct) }}
                </el-tag>
              </div>
              <strong v-else>现金观察</strong>
            </div>

            <div
              class="rotation-forward-metrics"
              data-testid="rotation-return-to-variance-metrics"
            >
              <div>
                <span>净清算收益</span>
                <strong>{{ formatSignedPercent(universeRotationReturnToVarianceChallenger.net_liquidation_return_pct) }}</strong>
              </div>
              <div>
                <span>相对原始排名</span>
                <strong>{{ formatSignedPercent(rotationReturnToVarianceDelta) }}</strong>
              </div>
              <div>
                <span>超额 QQQ</span>
                <strong>{{ formatSignedPercent(universeRotationReturnToVarianceChallenger.excess_return_vs_qqq_pct) }}</strong>
              </div>
              <div>
                <span>完整估算成本</span>
                <strong>{{ formatPercent(universeRotationReturnToVarianceChallenger.total_estimated_cost_pct) }}</strong>
              </div>
              <div>
                <span>前向会话</span>
                <strong>
                  {{ universeRotationReturnToVarianceChallenger.forward_observation_sessions }}/{{ universeRotationReturnToVarianceChallenger.elapsed_sessions }}
                </strong>
              </div>
            </div>

            <div class="rotation-forward-footer">
              <span>
                登记 {{ universeRotationReturnToVarianceChallenger.registered_as_of_date || '-' }}
                · {{ rotationVariantLabel(universeRotationReturnToVarianceChallenger.variant_name) }}
                · 并行只读
              </span>
              <strong v-if="universeRotationReturnToVarianceChallenger.evidence_mode === 'BACKFILLED_AFTER_ENTRY'">
                当前仅作回填对照；月末会按形成期收益/方差预登记下月组合。
              </strong>
              <strong v-else-if="universeRotationReturnToVarianceChallenger.evidence_mode === 'FORWARD_PRECOMMITTED'">
                已在入场前冻结；只验证排名方法，不改变当前组合或触发订单。
              </strong>
              <strong v-else>
                形成期波动证据暂不可用，不会回退到原始动量排名。
              </strong>
            </div>
          </section>

          <section
            class="rotation-scorecard"
            data-testid="rotation-forward-scorecard"
          >
            <div class="rotation-scorecard-header">
              <div>
                <div class="rotation-scorecard-title">
                  <strong>跨月前向记分牌</strong>
                  <el-tag type="info" size="small" effect="plain">完整月 3 期起</el-tag>
                  <el-tag type="warning" size="small" effect="plain">仅人工复核</el-tag>
                </div>
                <p>只累计入场前预登记并持有至月末的证据；回填结果不计分。</p>
              </div>
              <div v-if="rotationForwardScorecard" class="rotation-scorecard-meta">
                <span>截至 {{ rotationForwardScorecard.as_of_date }}</span>
                <span>Run #{{ rotationForwardScorecard.universe_run_id }}</span>
                <span>{{ rotationForwardScorecard.source_run_count }} 次历史运行</span>
              </div>
            </div>

            <el-alert
              v-if="rotationScorecardError"
              :title="rotationScorecardError"
              type="error"
              :closable="false"
              show-icon
              class="rotation-scorecard-alert"
              data-testid="rotation-forward-scorecard-error"
            />

            <div
              v-loading="rotationScorecardLoading && !rotationForwardScorecard"
              class="rotation-scorecard-content"
            >
              <template v-if="rotationScorecardRows.length">
                <div class="rotation-scorecard-table-view">
                  <el-table
                    :data="rotationScorecardRows"
                    size="small"
                    table-layout="fixed"
                    data-testid="rotation-forward-scorecard-table"
                  >
                    <el-table-column label="轨道" min-width="146">
                      <template #default="{ row }">
                        <div class="rotation-scorecard-variant">
                          <strong>{{ rotationVariantLabel(row.variant_name) }}</strong>
                          <small v-if="row.open_cohort">
                            {{ row.open_cohort.cohort_month.slice(0, 7) }} 采集中
                            · {{ formatSignedPercent(row.open_cohort.net_return_pct) }}
                          </small>
                          <small v-else-if="row.backfilled_cohorts">
                            {{ row.backfilled_cohorts }} 期回填已排除
                          </small>
                          <small v-else>尚无前向月份</small>
                        </div>
                      </template>
                    </el-table-column>
                    <el-table-column label="状态" width="104">
                      <template #default="{ row }">
                        <el-tag
                          :type="rotationScorecardStatusMeta(row.status).type"
                          size="small"
                          effect="plain"
                        >
                          {{ rotationScorecardStatusMeta(row.status).label }}
                        </el-tag>
                      </template>
                    </el-table-column>
                    <el-table-column label="完整月" width="76" align="center">
                      <template #default="{ row }">
                        <strong>{{ row.completed_cohorts }}/{{ row.minimum_completed_cohorts }}</strong>
                      </template>
                    </el-table-column>
                    <el-table-column label="累计收益" width="84" align="right">
                      <template #default="{ row }">
                        <strong :class="rotationScorecardMetricClass(row.compounded_return_pct)">
                          {{ formatSignedPercent(row.compounded_return_pct) }}
                        </strong>
                      </template>
                    </el-table-column>
                    <el-table-column label="累计超额" width="108">
                      <template #default="{ row }">
                        <div class="rotation-scorecard-pair">
                          <span>QQQ {{ formatSignedPercent(row.compounded_excess_vs_qqq_pct) }}</span>
                          <span>DIA {{ formatSignedPercent(row.compounded_excess_vs_dia_pct) }}</span>
                        </div>
                      </template>
                    </el-table-column>
                    <el-table-column label="超额月胜率" width="104">
                      <template #default="{ row }">
                        <div class="rotation-scorecard-pair">
                          <span>QQQ {{ formatPercent(row.excess_win_rate_vs_qqq_pct) }}</span>
                          <span>DIA {{ formatPercent(row.excess_win_rate_vs_dia_pct) }}</span>
                        </div>
                      </template>
                    </el-table-column>
                    <el-table-column label="复核门槛" min-width="154">
                      <template #default="{ row }">
                        <span
                          v-if="row.blockers.length || row.warnings.length"
                          class="rotation-scorecard-blockers"
                        >
                          {{ rotationScorecardEvidenceLabel(row) }}
                        </span>
                        <span v-else class="rotation-scorecard-clear">已满足，可人工复核</span>
                      </template>
                    </el-table-column>
                  </el-table>
                </div>

                <div
                  class="rotation-scorecard-mobile-list"
                  data-testid="rotation-forward-scorecard-mobile-list"
                >
                  <article
                    v-for="row in rotationScorecardRows"
                    :key="row.variant_name"
                    class="rotation-scorecard-mobile-row"
                  >
                    <div class="rotation-scorecard-mobile-heading">
                      <div>
                        <strong>{{ rotationVariantLabel(row.variant_name) }}</strong>
                        <small v-if="row.open_cohort">
                          {{ row.open_cohort.cohort_month.slice(0, 7) }}
                          · {{ formatSignedPercent(row.open_cohort.net_return_pct) }}
                        </small>
                        <small v-else>尚无完整前向持有月</small>
                      </div>
                      <el-tag
                        :type="rotationScorecardStatusMeta(row.status).type"
                        size="small"
                        effect="plain"
                      >
                        {{ rotationScorecardStatusMeta(row.status).label }}
                      </el-tag>
                    </div>
                    <div class="rotation-scorecard-mobile-metrics">
                      <div><span>完整月</span><strong>{{ row.completed_cohorts }}/{{ row.minimum_completed_cohorts }}</strong></div>
                      <div><span>累计收益</span><strong :class="rotationScorecardMetricClass(row.compounded_return_pct)">{{ formatSignedPercent(row.compounded_return_pct) }}</strong></div>
                      <div><span>超额 QQQ</span><strong>{{ formatSignedPercent(row.compounded_excess_vs_qqq_pct) }}</strong></div>
                      <div><span>超额 DIA</span><strong>{{ formatSignedPercent(row.compounded_excess_vs_dia_pct) }}</strong></div>
                      <div><span>胜率 QQQ/DIA</span><strong>{{ formatPercent(row.excess_win_rate_vs_qqq_pct) }} / {{ formatPercent(row.excess_win_rate_vs_dia_pct) }}</strong></div>
                    </div>
                    <p>{{ rotationScorecardEvidenceLabel(row) }}</p>
                  </article>
                </div>
              </template>

              <DataState
                v-else-if="!rotationScorecardLoading && !rotationScorecardError"
                empty
                empty-text="尚无跨月前向证据"
              />
            </div>

            <div class="rotation-scorecard-note" data-testid="rotation-forward-scorecard-note">
              至少 3 个完整月、累计收益为正、同时跑赢 QQQ 与 DIA，且两项超额月胜率均不低于 60%；达标后也只开放人工复核。
            </div>
          </section>

          <section
            v-if="universeRotationEvaluation"
            class="rotation-evaluation"
            data-testid="rotation-walk-forward"
          >
            <div class="rotation-evaluation-header">
              <div>
                <div class="rotation-evaluation-title">
                  <strong>月频 Walk-forward</strong>
                  <el-tag
                    :type="universeRotationEvaluation.status === 'COMPLETE' ? 'success' : 'warning'"
                    size="small"
                    effect="plain"
                  >
                    {{ rotationWalkForwardStatusLabel(universeRotationEvaluation.status) }}
                  </el-tag>
                  <el-tag type="info" size="small" effect="plain">当前成分股样本</el-tag>
                </div>
                <small>{{ universeRotationEvaluation.algorithm_version }}</small>
              </div>
              <div class="rotation-evaluation-selection">
                <span>
                  训练优胜
                  <strong data-testid="rotation-training-winner">
                    {{ rotationVariantLabel(universeRotationEvaluation.selected_variant) }}
                  </strong>
                </span>
                <span>
                  稳健挑战者
                  <strong data-testid="rotation-validated-challenger">
                    {{ rotationVariantLabel(universeRotationEvaluation.validated_challenger_variant) }}
                  </strong>
                </span>
              </div>
            </div>

            <div
              v-if="rotationDisplayVariant"
              class="rotation-evaluation-metrics"
              data-testid="rotation-validation-metrics"
            >
              <div>
                <span>留出年化</span>
                <strong>{{ formatSignedPercent(rotationDisplayVariant.validation.annualized_return_pct) }}</strong>
              </div>
              <div>
                <span>多窗年化</span>
                <strong>
                  {{ formatSignedPercent(
                    rotationDisplayVariant.expanding_validation?.annualized_return_pct
                      ?? rotationDisplayVariant.validation.annualized_return_pct,
                  ) }}
                </strong>
              </div>
              <div>
                <span>多窗超额 QQQ</span>
                <strong>
                  {{ formatSignedPercent(
                    rotationDisplayVariant.expanding_validation?.excess_annualized_return_vs_qqq_pct
                      ?? rotationDisplayVariant.validation.excess_annualized_return_vs_qqq_pct,
                  ) }}
                </strong>
              </div>
              <div>
                <span>多窗 Sharpe</span>
                <strong>
                  {{ formatDecimal(
                    rotationDisplayVariant.expanding_validation?.sharpe
                      ?? rotationDisplayVariant.validation.sharpe,
                  ) }}
                </strong>
              </div>
              <div>
                <span>多窗最大回撤</span>
                <strong>
                  {{ formatPercent(
                    rotationDisplayVariant.expanding_validation?.max_drawdown_pct
                      ?? rotationDisplayVariant.validation.max_drawdown_pct,
                  ) }}
                </strong>
              </div>
              <div>
                <span>窗口通过</span>
                <strong>
                  {{ rotationDisplayVariant.expanding_folds_passed ?? 0 }}/{{ rotationDisplayVariant.expanding_folds_total ?? 0 }}
                </strong>
              </div>
            </div>

            <div class="rotation-evaluation-footer">
              <span v-if="rotationDisplayVariant">
                训练 {{ rotationDisplayVariant.training.periods }} 月
                · 训练分 {{ formatDecimal(rotationDisplayVariant.training_score) }}
                · 留出 {{ rotationDisplayVariant.validation.periods }} 月
                · 多窗 {{ rotationDisplayVariant.expanding_validation?.periods ?? 0 }} 月
                · 留出成本 {{ formatPercent(rotationDisplayVariant.validation.total_cost_pct) }}
              </span>
              <strong>
                当前成分股存在幸存者偏差，仍需前向样本；不会自动晋级或下单。
              </strong>
            </div>
          </section>

          <section
            v-if="universeRotationPointInTime"
            class="rotation-evaluation rotation-point-in-time"
            data-testid="rotation-point-in-time"
          >
            <div class="rotation-evaluation-header">
              <div>
                <div class="rotation-evaluation-title">
                  <strong>点时成分敏感性</strong>
                  <el-tag
                    :type="universeRotationPointInTime.status === 'COMPLETE' ? 'success' : 'warning'"
                    size="small"
                    effect="plain"
                  >
                    {{ rotationWalkForwardStatusLabel(universeRotationPointInTime.status) }}
                  </el-tag>
                  <el-tag type="warning" size="small" effect="plain">部分消偏</el-tag>
                </div>
                <small>{{ universeRotationPointInTime.membership_history.source_version }}</small>
              </div>
              <div class="rotation-evaluation-selection">
                <span>
                  权威覆盖
                  <strong data-testid="rotation-point-in-time-coverage">
                    {{ universeRotationPointInTime.membership_history.authoritative_symbols }}/{{ universeRotationPointInTime.membership_history.catalog_size }}
                  </strong>
                </span>
                <span>
                  生效起点
                  <strong>{{ universeRotationPointInTime.membership_history.effective_start_date }}</strong>
                </span>
              </div>
            </div>

            <div
              v-if="rotationPointInTimeConcentrated || rotationPointInTimeEqual || rotationPointInTimeShrinkage || rotationPointInTimeInverse || rotationPointInTimeReturnToVariance"
              class="rotation-point-in-time-variants"
              data-testid="rotation-point-in-time-metrics"
            >
              <div
                v-if="rotationPointInTimeConcentrated"
                class="rotation-point-in-time-variant"
              >
                <div class="rotation-point-in-time-variant-title">
                  <strong>集中 Top6</strong>
                  <small>每风险组最多 2 只</small>
                </div>
                <span>
                  多窗年化
                  <strong>{{ formatSignedPercent(
                    rotationPointInTimeConcentrated.expanding_validation?.annualized_return_pct
                      ?? rotationPointInTimeConcentrated.validation.annualized_return_pct,
                  ) }}</strong>
                </span>
                <span>
                  Sharpe
                  <strong>{{ formatDecimal(
                    rotationPointInTimeConcentrated.expanding_validation?.sharpe
                      ?? rotationPointInTimeConcentrated.validation.sharpe,
                  ) }}</strong>
                </span>
                <span>
                  最大回撤
                  <strong>{{ formatPercent(
                    rotationPointInTimeConcentrated.expanding_validation?.max_drawdown_pct
                      ?? rotationPointInTimeConcentrated.validation.max_drawdown_pct,
                  ) }}</strong>
                </span>
                <span>
                  窗口
                  <strong>{{ rotationPointInTimeConcentrated.expanding_folds_passed ?? 0 }}/{{ rotationPointInTimeConcentrated.expanding_folds_total ?? 0 }}</strong>
                </span>
              </div>
              <div
                v-if="rotationPointInTimeEqual"
                class="rotation-point-in-time-variant"
              >
                <div class="rotation-point-in-time-variant-title">
                  <strong>等权 Top8</strong>
                  <small>按信号日成分过滤</small>
                </div>
                <span>
                  多窗年化
                  <strong>{{ formatSignedPercent(
                    rotationPointInTimeEqual.expanding_validation?.annualized_return_pct
                      ?? rotationPointInTimeEqual.validation.annualized_return_pct,
                  ) }}</strong>
                </span>
                <span>
                  Sharpe
                  <strong>{{ formatDecimal(
                    rotationPointInTimeEqual.expanding_validation?.sharpe
                      ?? rotationPointInTimeEqual.validation.sharpe,
                  ) }}</strong>
                </span>
                <span>
                  最大回撤
                  <strong>{{ formatPercent(
                    rotationPointInTimeEqual.expanding_validation?.max_drawdown_pct
                      ?? rotationPointInTimeEqual.validation.max_drawdown_pct,
                  ) }}</strong>
                </span>
                <span>
                  窗口
                  <strong>{{ rotationPointInTimeEqual.expanding_folds_passed ?? 0 }}/{{ rotationPointInTimeEqual.expanding_folds_total ?? 0 }}</strong>
                </span>
              </div>
              <div
                v-if="rotationPointInTimeShrinkage"
                class="rotation-point-in-time-variant"
              >
                <div class="rotation-point-in-time-variant-title">
                  <strong>收缩配权 Top8</strong>
                  <small>75% 等权 + 25% 逆波动</small>
                </div>
                <span>
                  多窗年化
                  <strong>{{ formatSignedPercent(
                    rotationPointInTimeShrinkage.expanding_validation?.annualized_return_pct
                      ?? rotationPointInTimeShrinkage.validation.annualized_return_pct,
                  ) }}</strong>
                </span>
                <span>
                  Sharpe
                  <strong>{{ formatDecimal(
                    rotationPointInTimeShrinkage.expanding_validation?.sharpe
                      ?? rotationPointInTimeShrinkage.validation.sharpe,
                  ) }}</strong>
                </span>
                <span>
                  最大回撤
                  <strong>{{ formatPercent(
                    rotationPointInTimeShrinkage.expanding_validation?.max_drawdown_pct
                      ?? rotationPointInTimeShrinkage.validation.max_drawdown_pct,
                  ) }}</strong>
                </span>
                <span>
                  窗口
                  <strong>{{ rotationPointInTimeShrinkage.expanding_folds_passed ?? 0 }}/{{ rotationPointInTimeShrinkage.expanding_folds_total ?? 0 }}</strong>
                </span>
              </div>
              <div
                v-if="rotationPointInTimeInverse"
                class="rotation-point-in-time-variant"
              >
                <div class="rotation-point-in-time-variant-title">
                  <strong>波动配权 Top8</strong>
                  <small>25% 单票上限</small>
                </div>
                <span>
                  多窗年化
                  <strong>{{ formatSignedPercent(
                    rotationPointInTimeInverse.expanding_validation?.annualized_return_pct
                      ?? rotationPointInTimeInverse.validation.annualized_return_pct,
                  ) }}</strong>
                </span>
                <span>
                  Sharpe
                  <strong>{{ formatDecimal(
                    rotationPointInTimeInverse.expanding_validation?.sharpe
                      ?? rotationPointInTimeInverse.validation.sharpe,
                  ) }}</strong>
                </span>
                <span>
                  最大回撤
                  <strong>{{ formatPercent(
                    rotationPointInTimeInverse.expanding_validation?.max_drawdown_pct
                      ?? rotationPointInTimeInverse.validation.max_drawdown_pct,
                  ) }}</strong>
                </span>
                <span>
                  窗口
                  <strong>{{ rotationPointInTimeInverse.expanding_folds_passed ?? 0 }}/{{ rotationPointInTimeInverse.expanding_folds_total ?? 0 }}</strong>
                </span>
              </div>
              <div
                v-if="rotationPointInTimeReturnToVariance"
                class="rotation-point-in-time-variant"
              >
                <div class="rotation-point-in-time-variant-title">
                  <strong>收益/方差 Top8</strong>
                  <small>形成期方差惩罚</small>
                </div>
                <span>
                  多窗年化
                  <strong>{{ formatSignedPercent(
                    rotationPointInTimeReturnToVariance.expanding_validation?.annualized_return_pct
                      ?? rotationPointInTimeReturnToVariance.validation.annualized_return_pct,
                  ) }}</strong>
                </span>
                <span>
                  Sharpe
                  <strong>{{ formatDecimal(
                    rotationPointInTimeReturnToVariance.expanding_validation?.sharpe
                      ?? rotationPointInTimeReturnToVariance.validation.sharpe,
                  ) }}</strong>
                </span>
                <span>
                  最大回撤
                  <strong>{{ formatPercent(
                    rotationPointInTimeReturnToVariance.expanding_validation?.max_drawdown_pct
                      ?? rotationPointInTimeReturnToVariance.validation.max_drawdown_pct,
                  ) }}</strong>
                </span>
                <span>
                  窗口
                  <strong>{{ rotationPointInTimeReturnToVariance.expanding_folds_passed ?? 0 }}/{{ rotationPointInTimeReturnToVariance.expanding_folds_total ?? 0 }}</strong>
                </span>
              </div>
            </div>

            <div class="rotation-evaluation-footer">
              <span>
                信号日排除尚未入指的股票
                · 快照补录 {{ universeRotationPointInTime.membership_history.snapshot_only_symbols.length }}
                · 缺失 {{ universeRotationPointInTime.membership_history.missing_symbols.length }}
              </span>
              <strong>
                历史退市或已调出标的仍未补齐，仅作敏感性诊断，不参与晋级或下单。
              </strong>
            </div>
          </section>

          <div class="universe-table-view">
            <el-table
              :data="universeRows"
              max-height="440"
              style="width: 100%"
              data-testid="universe-table"
            >
              <el-table-column label="候选" width="180">
                <template #default="{ row }">
                  <div class="universe-symbol">
                    <strong>{{ row.symbol }}</strong>
                    <small>{{ row.alias || row.sector || '-' }}</small>
                    <div class="universe-memberships">
                      <el-tag
                        v-for="membership in row.memberships"
                        :key="membership"
                        size="small"
                        effect="plain"
                      >
                        {{ membershipLabel(membership) }}
                      </el-tag>
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="126">
                <template #default="{ row }">
                  <div class="universe-state-tags">
                    <el-tag v-if="row.is_trading_target" type="danger" size="small">
                      当前实盘
                    </el-tag>
                    <el-tag :type="row.selected ? 'success' : 'info'" size="small" effect="plain">
                      {{ row.selected ? '候选入选' : '未入选' }}
                    </el-tag>
                    <el-tag v-if="row.exploration_selected" type="warning" size="small" effect="plain">
                      探索观察
                    </el-tag>
                    <el-tag v-else-if="row.shadow_enabled" type="warning" size="small" effect="plain">
                      Shadow 已启用
                    </el-tag>
                    <el-tag v-if="row.metrics.rotation?.selected" size="small" effect="plain">
                      轮动影子
                    </el-tag>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="排名/轮动" width="96" align="right">
                <template #default="{ row }">
                  <div class="universe-rank">
                    <span>
                      <strong>{{ row.rank ? `#${row.rank}` : '-' }}</strong>
                      <small class="universe-score">{{ formatScore(row.score) }}</small>
                    </span>
                    <span v-if="row.metrics.rotation">
                      <small>轮动 {{ row.metrics.rotation.rank ? `#${row.metrics.rotation.rank}` : '-' }}</small>
                      <small class="universe-score">
                        {{ formatSignedPercent(row.metrics.rotation.momentum_pct) }}
                      </small>
                    </span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="日均流动性" width="110" align="right">
                <template #default="{ row }">{{ formatDollarVolume(row.metrics.avg_dollar_volume) }}</template>
              </el-table-column>
              <el-table-column label="成本/波动" width="130" align="right">
                <template #default="{ row }">
                  <div class="universe-risk-metrics">
                    <span><small>成本</small>{{ formatBps(row.metrics.relative_spread_bps) }}</span>
                    <span><small>波动</small>{{ formatVolatility(row.metrics.realized_vol_20d) }}</span>
                    <span><small>ATR</small>{{ formatAtr(row.metrics.atr_pct_14d) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="筛选结论" min-width="180">
                <template #default="{ row }">
                  <div v-if="row.exclusion_reasons.length" class="universe-reasons">
                    <el-tag
                      v-for="reason in row.exclusion_reasons"
                      :key="reason"
                      type="info"
                      size="small"
                      effect="plain"
                    >
                      {{ exclusionReasonLabel(reason) }}
                    </el-tag>
                  </div>
                  <span v-else class="universe-pass">通过硬性门槛</span>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div class="universe-mobile-list" data-testid="universe-mobile-list">
            <article v-for="row in universeRows" :key="row.symbol" class="universe-mobile-row">
              <div class="universe-mobile-heading">
                <div>
                  <strong>{{ row.symbol }}</strong>
                  <small>{{ row.alias || row.sector || '-' }}</small>
                </div>
                <div class="universe-state-tags">
                  <el-tag v-if="row.is_trading_target" type="danger" size="small">当前实盘</el-tag>
                  <el-tag :type="row.selected ? 'success' : 'info'" size="small" effect="plain">
                    {{ row.selected ? `候选 #${row.rank ?? '-'}` : '未入选' }}
                  </el-tag>
                  <el-tag v-if="row.exploration_selected" type="warning" size="small" effect="plain">
                    探索观察
                  </el-tag>
                  <el-tag v-else-if="row.shadow_enabled" type="warning" size="small" effect="plain">
                    Shadow 已启用
                  </el-tag>
                  <el-tag v-if="row.metrics.rotation?.selected" size="small" effect="plain">
                    轮动影子
                  </el-tag>
                </div>
              </div>
              <div class="universe-mobile-memberships">
                <el-tag
                  v-for="membership in row.memberships"
                  :key="membership"
                  size="small"
                  effect="plain"
                >
                  {{ membershipLabel(membership) }}
                </el-tag>
              </div>
              <div class="universe-mobile-metrics">
                <div><span>分数</span><strong>{{ formatScore(row.score) }}</strong></div>
                <div><span>流动性</span><strong>{{ formatDollarVolume(row.metrics.avg_dollar_volume) }}</strong></div>
                <div><span>T-1 成本</span><strong>{{ formatBps(row.metrics.relative_spread_bps) }}</strong></div>
                <div><span>波动</span><strong>{{ formatVolatility(row.metrics.realized_vol_20d) }}</strong></div>
                <div><span>ATR</span><strong>{{ formatAtr(row.metrics.atr_pct_14d) }}</strong></div>
                <div><span>轮动排名</span><strong>{{ row.metrics.rotation?.rank ? `#${row.metrics.rotation.rank}` : '-' }}</strong></div>
                <div><span>12-1 动量</span><strong>{{ formatSignedPercent(row.metrics.rotation?.momentum_pct) }}</strong></div>
              </div>
              <div class="universe-mobile-reason">
                {{ row.exclusion_reasons.length
                  ? row.exclusion_reasons.map(exclusionReasonLabel).join(' · ')
                  : '通过硬性门槛' }}
              </div>
            </article>
          </div>

          <div class="universe-footer">
            <span>
              完成于 {{ formatDateTime(universeRun.completed_at || universeRun.created_at) }}
              · 来源 {{ universeRun.source_version }}
            </span>
            <span>轮动结果仅是月频影子证据；当前实盘标的仍由下方“交易中”状态明确标识。</span>
          </div>
        </template>

        <DataState
          v-else-if="!universeLoading"
          empty
          :empty-text="universeCatalog.length
            ? `候选目录已加载 ${universeCatalog.length} 个标的，尚无筛选记录`
            : '尚无动态候选池记录'"
        />
      </div>

      <section class="promotion-readiness" data-testid="promotion-readiness">
        <div class="promotion-header">
          <div class="promotion-heading">
            <div class="promotion-title">
              <strong>前瞻证据</strong>
              <el-tag type="warning" size="small" effect="plain">仅人工升级</el-tag>
              <el-tag type="info" size="small" effect="plain">不自动切换</el-tag>
            </div>
            <p>前向样本只用于人工复核；证据达标也不会自动修改当前实盘标的。</p>
          </div>
          <div v-if="promotionReadiness" class="promotion-meta">
            <span>Run #{{ promotionReadiness.universe_run_id }}</span>
            <span>{{ promotionReadiness.as_of_date }}</span>
            <span>生成 {{ formatDateTime(promotionReadiness.generated_at) }}</span>
          </div>
        </div>

        <el-alert
          v-if="promotionError"
          :title="promotionError"
          type="error"
          :closable="false"
          show-icon
          class="promotion-alert"
          data-testid="promotion-readiness-error"
        />

        <div
          v-loading="promotionLoading && !promotionReadiness"
          class="promotion-content"
        >
          <template v-if="promotionReadiness?.items.length">
            <div class="promotion-table-view">
              <el-table
                :data="promotionRows"
                size="small"
                max-height="360"
                style="width: 100%"
                data-testid="promotion-readiness-table"
              >
                <el-table-column label="池内标的" width="124">
                  <template #default="{ row }">
                    <div class="promotion-symbol">
                      <strong>{{ row.symbol }}</strong>
                      <div class="promotion-badges">
                        <el-tag
                          v-if="row.is_trading_target"
                          type="danger"
                          size="small"
                          data-testid="promotion-trading-badge"
                        >
                          当前实盘
                        </el-tag>
                        <el-tag
                          v-if="row.universe_role === 'EXPLORATION'"
                          type="info"
                          size="small"
                          effect="plain"
                        >
                          探索
                        </el-tag>
                        <el-tag
                          v-if="row.shadow_enabled"
                          type="warning"
                          size="small"
                          effect="plain"
                        >
                          Shadow
                        </el-tag>
                      </div>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="融合优先" width="96" align="right">
                  <template #default="{ row }">
                    <strong>#{{ row.priority_rank }}</strong>
                    <small class="promotion-secondary">
                      {{ formatScore(row.priority_score) }}
                      · {{ promotionSourceLabel(row) }}
                    </small>
                  </template>
                </el-table-column>
                <el-table-column label="量化适配" width="116">
                  <template #default="{ row }">
                    <div
                      class="promotion-quant"
                      :title="promotionQuantTitle(row)"
                      data-testid="promotion-quant-fit"
                    >
                      <div>
                        <strong>{{ promotionQuantScoreLabel(row) }}</strong>
                        <span>{{ promotionQuantOutcomeLabel(row) }}</span>
                      </div>
                      <el-tag
                        v-if="promotionQuantState(row) === 'ERROR'"
                        type="warning"
                        size="small"
                        effect="plain"
                        data-testid="promotion-quant-error"
                      >
                        数据异常
                      </el-tag>
                      <el-tag
                        v-else-if="promotionQuantState(row) === 'STALE'"
                        type="danger"
                        size="small"
                        effect="plain"
                        data-testid="promotion-quant-stale"
                      >
                        已过期
                      </el-tag>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="前瞻状态" width="112">
                  <template #default="{ row }">
                    <el-tag
                      :type="promotionStatusMeta(row.forward_status).type"
                      size="small"
                      effect="plain"
                    >
                      {{ promotionStatusMeta(row.forward_status).label }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="证据进度" width="128">
                  <template #default="{ row }">
                    <div class="promotion-progress">
                      <div>
                        <strong>{{ row.included_pairs }}/{{ row.minimum_mature_pairs }}</strong>
                        <span>距复核 {{ row.remaining_ready_pairs }}</span>
                      </div>
                      <el-progress
                        :percentage="promotionProgressPercent(
                          row.included_pairs,
                          row.minimum_mature_pairs,
                        )"
                        :stroke-width="5"
                        :show-text="false"
                      />
                      <small>距成熟 {{ row.remaining_mature_pairs }}</small>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="净 PnL" width="126" align="right">
                  <template #default="{ row }">
                    <div class="promotion-comparison">
                      <span>候选 <strong :class="pnlClass(row.candidate_metrics.net_pnl)">{{ formatSignedPnl(row.candidate_metrics.net_pnl) }}</strong></span>
                      <span>基线 <strong :class="pnlClass(row.baseline_metrics.net_pnl)">{{ formatSignedPnl(row.baseline_metrics.net_pnl) }}</strong></span>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="闭合交易" width="94" align="right">
                  <template #default="{ row }">
                    <div class="promotion-comparison">
                      <span>候选 <strong>{{ row.candidate_metrics.closed_trades }}</strong></span>
                      <span>基线 <strong>{{ row.baseline_metrics.closed_trades }}</strong></span>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="阻塞项" min-width="140">
                  <template #default="{ row }">
                    <div
                      v-if="row.blockers.length"
                      class="promotion-blockers"
                      data-testid="promotion-blockers"
                    >
                      {{ row.blockers.map(promotionBlockerLabel).join('；') }}
                    </div>
                    <span v-else class="promotion-clear">无阻塞</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>

            <div class="promotion-mobile-list" data-testid="promotion-readiness-mobile-list">
              <article
                v-for="row in promotionRows"
                :key="row.symbol"
                class="promotion-mobile-row"
              >
                <div class="promotion-mobile-heading">
                  <div>
                    <strong>{{ row.symbol }}</strong>
                    <small>
                      融合 #{{ row.priority_rank }}
                      · {{ formatScore(row.priority_score) }}
                      · {{ promotionSourceLabel(row) }}
                    </small>
                  </div>
                  <div class="promotion-mobile-tags">
                    <el-tag v-if="row.is_trading_target" type="danger" size="small">当前实盘</el-tag>
                    <el-tag
                      v-if="row.universe_role === 'EXPLORATION'"
                      type="info"
                      size="small"
                      effect="plain"
                    >
                      探索
                    </el-tag>
                    <el-tag
                      :type="promotionStatusMeta(row.forward_status).type"
                      size="small"
                      effect="plain"
                    >
                      {{ promotionStatusMeta(row.forward_status).label }}
                    </el-tag>
                  </div>
                </div>
                <div class="promotion-mobile-metrics">
                  <div>
                    <span>量化适配</span>
                    <strong>
                      {{ promotionQuantScoreLabel(row) }}
                      · {{ promotionQuantOutcomeLabel(row) }}
                    </strong>
                    <el-tag
                      v-if="promotionQuantState(row) === 'ERROR'"
                      type="warning"
                      size="small"
                      effect="plain"
                    >
                      数据异常
                    </el-tag>
                    <el-tag
                      v-else-if="promotionQuantState(row) === 'STALE'"
                      type="danger"
                      size="small"
                      effect="plain"
                    >
                      已过期
                    </el-tag>
                  </div>
                  <div><span>证据进度</span><strong>{{ row.included_pairs }}/{{ row.minimum_mature_pairs }}</strong></div>
                  <div><span>距复核/成熟</span><strong>{{ row.remaining_ready_pairs }}/{{ row.remaining_mature_pairs }}</strong></div>
                  <div><span>候选净 PnL</span><strong :class="pnlClass(row.candidate_metrics.net_pnl)">{{ formatSignedPnl(row.candidate_metrics.net_pnl) }}</strong></div>
                  <div><span>基线净 PnL</span><strong :class="pnlClass(row.baseline_metrics.net_pnl)">{{ formatSignedPnl(row.baseline_metrics.net_pnl) }}</strong></div>
                  <div><span>闭合交易 候选/基线</span><strong>{{ row.candidate_metrics.closed_trades }}/{{ row.baseline_metrics.closed_trades }}</strong></div>
                </div>
                <div class="promotion-mobile-blockers">
                  <span>阻塞项</span>
                  <strong>
                    {{ row.blockers.length
                      ? row.blockers.map(promotionBlockerLabel).join('；')
                      : '无阻塞' }}
                  </strong>
                </div>
              </article>
            </div>
          </template>

          <DataState
            v-else-if="!promotionLoading && !promotionError"
            empty
            empty-text="当前候选池尚无前瞻证据"
          />
        </div>

        <div class="promotion-manual-note" data-testid="promotion-manual-note">
          晋级与实盘切换始终由人工复核后执行，系统不提供自动升级或自动切换。
        </div>
      </section>
    </el-card>

    <el-card style="margin-bottom: 20px">
      <el-form :inline="true" @submit.prevent="handleAdd">
        <el-form-item label="股票代码">
          <el-input v-model="newSymbol" placeholder="例如 AAPL.US" style="width: 180px" />
        </el-form-item>
        <el-form-item label="市场">
          <el-radio-group v-model="newMarket">
            <el-radio value="US">美股</el-radio>
            <el-radio value="HK">港股</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="别名">
          <el-input v-model="newAlias" placeholder="可选别名" style="width: 140px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="adding" :disabled="!newSymbol.trim()" @click="handleAdd">
            添加
          </el-button>
        </el-form-item>
      </el-form>
      <div v-if="addError" style="margin-top: 8px">
        <el-alert :title="addError" type="error" :closable="false" show-icon />
      </div>
    </el-card>

    <el-card>
      <div class="watchlist-toolbar">
        <span class="watchlist-toolbar-note">{{ items.length }} 个标的 · 行情每 15s 刷新</span>
        <span class="watchlist-toolbar-note" data-testid="watchlist-last-refresh">行情最近成功刷新：{{ lastRefreshLabel }}</span>
        <el-button
          size="small"
          plain
          :disabled="quantRanking || scoringSymbol !== null"
          data-testid="watchlist-refresh-now"
          @click="refreshNow"
        >
          手动刷新
        </el-button>
        <el-button
          size="small"
          type="primary"
          plain
          :icon="DataAnalysis"
          :loading="quantRanking"
          :disabled="items.length === 0"
          data-testid="watchlist-quant-rank"
          @click="handleQuantRank"
        >
          量化评分
        </el-button>
        <el-button
          size="small"
          plain
          :disabled="items.length === 0"
          data-testid="watchlist-export-csv"
          @click="exportSnapshot"
        >
          导出快照 CSV
        </el-button>
      </div>
      <div class="watchlist-filters">
        <el-input v-model="searchText" placeholder="搜索代码/别名" clearable style="width: 180px" data-testid="watchlist-search" data-view-search="true" />
        <el-select v-model="marketFilter" placeholder="全部市场" clearable style="width: 120px" data-testid="watchlist-market-filter">
          <el-option label="US" value="US" />
          <el-option label="HK" value="HK" />
        </el-select>
        <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 130px" data-testid="watchlist-status-filter">
          <el-option label="交易中" value="active" />
          <el-option label="观察中" value="watching" />
        </el-select>
        <el-select v-model="scoreBucket" placeholder="全部评分" clearable style="width: 140px" data-testid="watchlist-score-filter">
          <el-option label="量化优选" value="high" />
          <el-option label="量化观察" value="mid" />
          <el-option label="量化回避" value="low" />
        </el-select>
        <el-select v-model="sortMode" style="width: 150px" data-testid="watchlist-sort-mode">
          <el-option label="默认排序" value="default" />
          <el-option label="评分从高到低" value="score_desc" />
          <el-option label="价差从小到大" value="spread_asc" />
          <el-option label="最新价从高到低" value="price_desc" />
        </el-select>
        <el-button :type="hideStaleScores ? 'primary' : ''" data-testid="watchlist-hide-stale" @click="hideStaleScores = !hideStaleScores">隐藏过期评分</el-button>
        <el-button data-testid="watchlist-clear-filters" @click="clearFilters">清空筛选</el-button>
      </div>
      <div class="watchlist-filter-summary" data-testid="watchlist-filter-summary">
        当前显示 {{ filteredItems.length }}/{{ items.length }}
        <span v-if="searchText.trim()"> · 搜索 {{ searchText.trim() }}</span>
        <span v-if="marketFilter"> · 市场 {{ marketFilter }}</span>
        <span v-if="statusFilter"> · 状态 {{ statusFilter === 'active' ? '交易中' : '观察中' }}</span>
        <span v-if="scoreBucket"> · 评分 {{ scoreBucketLabel }}</span>
        <span v-if="sortMode !== 'default'"> · 排序 {{ sortModeLabel }}</span>
        <span v-if="hideStaleScores"> · 隐藏过期评分</span>
      </div>
      <div class="watchlist-bulk-actions">
        <el-checkbox :model-value="allFilteredSelected" data-testid="watchlist-select-all" @change="toggleSelectAll">全选当前结果</el-checkbox>
        <span data-testid="watchlist-selection-summary">已选择 {{ selectedIds.length }}</span>
        <el-button size="small" :disabled="selectedRows.length === 0" data-testid="watchlist-bulk-export" @click="exportSelected">导出所选 CSV</el-button>
        <el-button size="small" type="danger" :disabled="selectedRows.length === 0" data-testid="watchlist-bulk-delete" @click="bulkDeleteDialog = true">批量删除</el-button>
      </div>
      <div class="watchlist-table-scroll">
      <el-table :data="filteredItems" v-loading="loading" style="width: 100%" data-testid="watchlist-table">
        <el-table-column width="48">
          <template #default="{ row }">
            <el-checkbox :model-value="selectedIds.includes(row.id)" @change="toggleSelection(row.id)" />
          </template>
        </el-table-column>
        <el-table-column prop="symbol" label="代码" width="120" />
        <el-table-column prop="market" label="市场" width="80">
          <template #default="{ row }">
            <el-tag size="small">{{ row.market }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="alias" label="别名" width="140">
          <template #default="{ row }">
            <div>{{ row.alias || '-' }}</div>
            <small class="watchlist-source">{{ watchlistSourceLabel(row.source) }}</small>
          </template>
        </el-table-column>
        <el-table-column
          label="候选评估"
          width="270"
          sortable
          :sort-by="(row: WatchlistItem) => scoreMap[row.symbol]?.score ?? -1"
        >
          <template #default="{ row }">
            <div class="score-stack">
              <div
                v-if="scoreMap[row.symbol]"
                class="score-channel"
                data-testid="watchlist-quant-score"
              >
                <span class="score-channel-label">量化主分</span>
                <el-tag
                  :type="scoreTagType(scoreMap[row.symbol])"
                  size="small"
                  class="score-tag"
                  data-testid="watchlist-score-tag"
                  @click="openScoreDrawer(scoreMap[row.symbol])"
                >
                  {{ scoreMap[row.symbol].score.toFixed(0) }}
                </el-tag>
                <span class="score-outcome">{{ scoreOutcomeLabel(scoreMap[row.symbol]) }}</span>
                <span class="score-source">{{ scoreSourceLabel(scoreMap[row.symbol].source) }}</span>
                <el-tag
                  v-if="isScoreStale(scoreMap[row.symbol])"
                  type="warning"
                  size="small"
                  data-testid="watchlist-stale-badge"
                >
                  已过期
                </el-tag>
              </div>
              <div v-else class="score-channel score-channel-empty">
                <span class="score-channel-label">量化主分</span>
                <span>未评分</span>
              </div>
              <div
                v-if="reviewMap[row.symbol]"
                class="score-channel"
                data-testid="watchlist-review-score"
              >
                <span class="score-channel-label">AI 复核</span>
                <el-tag
                  :type="scoreTagType(reviewMap[row.symbol])"
                  size="small"
                  class="score-tag"
                  data-testid="watchlist-score-tag"
                  @click="openScoreDrawer(reviewMap[row.symbol])"
                >
                  {{ reviewMap[row.symbol].score.toFixed(0) }}
                </el-tag>
                <span class="score-outcome">{{ scoreOutcomeLabel(reviewMap[row.symbol]) }}</span>
                <span class="score-source">{{ scoreSourceLabel(reviewMap[row.symbol].source) }}</span>
                <el-tag
                  v-if="isScoreStale(reviewMap[row.symbol])"
                  type="warning"
                  size="small"
                  data-testid="watchlist-stale-badge"
                >
                  已过期
                </el-tag>
              </div>
              <div v-else class="score-channel score-channel-empty">
                <span class="score-channel-label">AI 复核</span>
                <span>未复核</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="行情" width="180">
          <template #default="{ row }">
            <div v-if="quoteMap[row.symbol]">
              <div>{{ formatCurrency(quoteMap[row.symbol].last_price, row.market) }}</div>
              <small style="color: #909399">
                Bid {{ formatCurrency(quoteMap[row.symbol].bid, row.market) }} / Ask {{ formatCurrency(quoteMap[row.symbol].ask, row.market) }}
              </small>
            </div>
            <div v-else style="color: #909399">-</div>
          </template>
        </el-table-column>
        <el-table-column
          label="价差"
          width="110"
          :sort-method="(a: WatchlistItem, b: WatchlistItem) => (spreadFor(a) ?? -1) - (spreadFor(b) ?? -1)"
          sortable
        >
          <template #default="{ row }">
            <span v-if="quoteMap[row.symbol] && spreadFor(row) !== null" data-testid="watchlist-spread">
              {{ formatCurrency(spreadFor(row), row.market) }}
            </span>
            <span v-else style="color: #909399">-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.is_trading_target" type="success">交易中</el-tag>
            <el-tag v-else type="info">观察中</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button
              size="small"
              data-testid="watchlist-copy-symbol"
              @click="copySymbol(row.symbol)"
            >复制</el-button>
            <el-button
              v-if="!row.is_trading_target"
              type="primary"
              size="small"
              :loading="activatingId === row.id"
              @click="handleActivate(row.id)"
            >
              设为交易
            </el-button>
            <el-button
              size="small"
              :loading="scoringSymbol === row.symbol"
              :disabled="scoringSymbol !== null && scoringSymbol !== row.symbol"
              :aria-label="`对 ${row.symbol} 进行 AI 复核`"
              @click="handleScore(row.symbol, row.market)"
            >
              AI 复核
            </el-button>
            <el-button
              type="danger"
              size="small"
              :loading="removingId === row.id"
              @click="handleRemove(row.id)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      </div>
      <div v-if="items.length > 0 && filteredItems.length === 0 && !loading" data-testid="watchlist-filter-empty" style="text-align: center; color: #909399; padding: 32px">
        没有匹配的观察标的，请调整筛选条件
      </div>
      <DataState
        v-if="items.length === 0 && !loading"
        empty
        empty-text="暂无观察标的，请添加股票代码"
      />
    </el-card>

    <el-dialog v-model="bulkDeleteDialog" title="确认批量删除" width="360px">
      <p>将删除当前可见已选的 {{ selectedRows.length }} 个观察标的。</p>
      <p class="bulk-delete-symbols">{{ selectedRows.map((row) => row.symbol).join(', ') }}</p>
      <template #footer>
        <el-button @click="bulkDeleteDialog = false">取消</el-button>
        <el-button type="danger" data-testid="watchlist-bulk-delete-confirm" @click="confirmBulkDelete">确认删除</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="scoreDrawer.visible"
      :title="`${scoreDrawer.score?.symbol || ''} ${scoreDrawerChannelLabel}详情`"
      size="380px"
      destroy-on-close
      data-testid="watchlist-score-drawer"
    >
      <template v-if="scoreDrawer.score">
        <div class="score-detail-header">
          <div class="score-detail-score" :class="scoreDetailClass">{{ scoreDrawer.score.score.toFixed(0) }}</div>
          <el-tag :type="scoreTagType(scoreDrawer.score)" size="small">{{ scoreOutcomeLabel(scoreDrawer.score) }}</el-tag>
        </div>
        <div class="score-detail-section">
          <div class="score-detail-label">评分依据</div>
          <p class="score-detail-rationale">{{ scoreDrawer.score.rationale || '暂无说明' }}</p>
        </div>
        <div class="score-detail-section">
          <div class="score-detail-label">置信度</div>
          <strong>{{ (scoreDrawer.score.confidence * 100).toFixed(0) }}%</strong>
        </div>
        <div class="score-detail-section">
          <div class="score-detail-label">来源</div>
          <el-tag :type="scoreSourceTagType(scoreDrawer.score.source)" size="small">
            {{ scoreSourceLabel(scoreDrawer.score.source) }}
          </el-tag>
          <el-tag v-if="isScoreStale(scoreDrawer.score)" type="warning" size="small">已过期</el-tag>
        </div>
        <div class="score-detail-section">
          <div class="score-detail-label">时间</div>
          <div>生成：{{ formatDateTime(scoreDrawer.score.created_at) }}</div>
          <div>过期：{{ formatDateTime(scoreDrawer.score.expires_at) }}</div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { DataAnalysis, Refresh } from '@element-plus/icons-vue'
import { isAxiosError } from 'axios'
import type {
  UniverseCatalogItem,
  UniverseIndexMembershipHistoryMetadata,
  UniversePromotionForwardStatus,
  UniversePromotionReadinessItem,
  UniversePromotionReadinessResponse,
  UniverseRotationPointInTimeSensitivity,
  UniverseRotationPerformance,
  UniverseRotationForwardHolding,
  UniverseRotationForwardScorecardResponse,
  UniverseRotationForwardScorecardStatus,
  UniverseRotationForwardSnapshot,
  UniverseRotationForwardTrackScore,
  UniverseRotationVariantConfig,
  UniverseRotationVariantEvaluation,
  UniverseRotationWalkForwardEvaluation,
  UniverseSelectionItem,
  UniverseSelectionRunResponse,
  WatchlistItem,
  WatchlistQuote,
} from '../types'
import {
  getWatchlist,
  addWatchlistItem,
  removeWatchlistItem,
  activateWatchlistItem,
  getWatchlistQuotes,
  getWatchlistScores,
  rankWatchlistQuant,
  scoreWatchlistSymbol,
  type WatchlistScore,
} from '../api/watchlist'
import {
  getLatestUniverseSelection,
  getRotationForwardScorecard,
  getUniversePromotionReadiness,
  getUniverseCatalog,
  refreshUniverseSelection,
} from '../api/universe'
import { formatCurrency } from '../utils/format'
import { resolveErrorMessage } from '../utils/error'
import DataState from '../components/DataState.vue'
import { useRegisterViewRefresh } from '../composables/useViewRefreshRegistry'
import { downloadCsv } from '../utils/csv'

const items = ref<WatchlistItem[]>([])
const quoteMap = ref<Record<string, WatchlistQuote>>({})
const scoreMap = ref<Record<string, WatchlistScore>>({})
const reviewMap = ref<Record<string, WatchlistScore>>({})
const loading = ref(false)
const adding = ref(false)
const quantRanking = ref(false)
const universeCatalog = ref<UniverseCatalogItem[]>([])
const universeRun = ref<UniverseSelectionRunResponse | null>(null)
const rotationForwardScorecard = ref<UniverseRotationForwardScorecardResponse | null>(null)
const rotationScorecardLoading = ref(false)
const rotationScorecardError = ref('')
const universeLoading = ref(false)
const universeRefreshing = ref(false)
const universeError = ref('')
const promotionReadiness = ref<UniversePromotionReadinessResponse | null>(null)
const promotionLoading = ref(false)
const promotionError = ref('')
const addError = ref('')
const activatingId = ref<number | null>(null)
const removingId = ref<number | null>(null)
const scoringSymbol = ref<string | null>(null)
const searchText = ref('')
const marketFilter = ref<'US' | 'HK' | ''>('')
const statusFilter = ref<'active' | 'watching' | ''>('')
const scoreBucket = ref<'high' | 'mid' | 'low' | ''>('')
const hideStaleScores = ref(false)
const sortMode = ref<'default' | 'score_desc' | 'spread_asc' | 'price_desc'>('default')
const selectedIds = ref<number[]>([])
const lastRefreshAt = ref<Date | null>(null)
const bulkDeleteDialog = ref(false)
const newSymbol = ref('')
const newMarket = ref<'US' | 'HK'>('US')
const newAlias = ref('')
const scoreClockMs = ref(Date.now())
let quoteTimer: ReturnType<typeof setInterval> | null = null
let scoreExpiryTimer: ReturnType<typeof setInterval> | null = null
// Consecutive quote-fetch failure count. After QUOTE_FAILURE_TOAST_THRESHOLD
// consecutive failures we stop spamming ElMessage.error to avoid drowning the UI.
let quoteFailureStreak = 0
const QUOTE_FAILURE_TOAST_THRESHOLD = 3
const QUOTE_FAILURE_TOAST_COOLDOWN_MS = 60_000
let lastQuoteFailureToastAt = 0
let universeRequestGeneration = 0
let rotationScorecardRequestGeneration = 0
let promotionRequestGeneration = 0
let quantScoreGeneration = 0
let reviewScoreGeneration = 0

const scoreDrawer = reactive({
  visible: false,
  score: null as WatchlistScore | null,
})

const scoreDetailClass = computed(() => {
  return scoreDrawer.score ? scoreVisualClass(scoreDrawer.score) : 'score-none'
})

const scoreDrawerChannelLabel = computed(() => (
  scoreDrawer.score && isQuantScore(scoreDrawer.score)
    ? '量化主分'
    : 'AI 复核'
))

const universeRows = computed<UniverseSelectionItem[]>(() => {
  if (!universeRun.value) return []
  return [...universeRun.value.items].sort((left, right) => {
    if (left.selected !== right.selected) return left.selected ? -1 : 1
    if (left.rank !== null && right.rank !== null) return left.rank - right.rank
    if (left.rank !== null) return -1
    if (right.rank !== null) return 1
    const leftRotation = left.metrics.rotation
    const rightRotation = right.metrics.rotation
    if (Boolean(leftRotation?.selected) !== Boolean(rightRotation?.selected)) {
      return leftRotation?.selected ? -1 : 1
    }
    if (
      leftRotation?.rank !== null
      && leftRotation?.rank !== undefined
      && rightRotation?.rank !== null
      && rightRotation?.rank !== undefined
    ) {
      return leftRotation.rank - rightRotation.rank
    }
    if (left.score !== right.score) return right.score - left.score
    return left.symbol.localeCompare(right.symbol)
  })
})

const universeExplorationCount = computed(() => (
  universeRun.value?.items.filter((item) => item.exploration_selected).length
  ?? 0
))

const universeRotationCount = computed(() => (
  universeRun.value?.items.filter((item) => item.metrics.rotation?.selected).length
  ?? 0
))

const universeRotationEvaluation = computed<UniverseRotationWalkForwardEvaluation | null>(() => {
  const raw = universeRun.value?.parameters.rotation_evaluation
  return isRotationWalkForwardEvaluation(raw) ? raw : null
})

const universeRotationPointInTime = computed<UniverseRotationPointInTimeSensitivity | null>(() => {
  const raw = universeRun.value?.parameters.rotation_point_in_time_sensitivity
  return isRotationPointInTimeSensitivity(raw) ? raw : null
})

const rotationPointInTimeConcentrated = computed<UniverseRotationVariantEvaluation | null>(() => (
  universeRotationPointInTime.value?.evaluation?.variants.find(
    (variant) => variant.variant.name === 'concentrated_top6_12_1',
  ) ?? null
))

const rotationPointInTimeEqual = computed<UniverseRotationVariantEvaluation | null>(() => (
  universeRotationPointInTime.value?.evaluation?.variants.find(
    (variant) => variant.variant.name === 'diversified_top8_12_1',
  ) ?? null
))

const rotationPointInTimeShrinkage = computed<UniverseRotationVariantEvaluation | null>(() => (
  universeRotationPointInTime.value?.evaluation?.variants.find(
    (variant) => variant.variant.name === 'diversified_top8_12_1_eq75_iv25_cap15',
  ) ?? null
))

const rotationPointInTimeInverse = computed<UniverseRotationVariantEvaluation | null>(() => (
  universeRotationPointInTime.value?.evaluation?.variants.find(
    (variant) => variant.variant.name === 'diversified_top8_12_1_inverse_vol_25',
  ) ?? null
))

const rotationPointInTimeReturnToVariance = computed<UniverseRotationVariantEvaluation | null>(() => (
  universeRotationPointInTime.value?.evaluation?.variants.find(
    (variant) => variant.variant.name === 'diversified_top8_12_1_return_to_variance',
  ) ?? null
))

const universeRotationForward = computed<UniverseRotationForwardSnapshot | null>(() => {
  const raw = universeRun.value?.parameters.rotation_forward_snapshot
  return isRotationForwardSnapshot(raw) ? raw : null
})

const universeRotationConcentrationChallenger = computed<UniverseRotationForwardSnapshot | null>(() => {
  const raw = universeRun.value?.parameters.rotation_concentration_challenger_snapshot
  return isRotationForwardSnapshot(raw) ? raw : null
})

const universeRotationWeightingChallenger = computed<UniverseRotationForwardSnapshot | null>(() => {
  const raw = universeRun.value?.parameters.rotation_weighting_challenger_snapshot
  return isRotationForwardSnapshot(raw) ? raw : null
})

const universeRotationShrinkageChallenger = computed<UniverseRotationForwardSnapshot | null>(() => {
  const raw = universeRun.value?.parameters.rotation_shrinkage_challenger_snapshot
  return isRotationForwardSnapshot(raw) ? raw : null
})

const universeRotationReturnToVarianceChallenger = computed<UniverseRotationForwardSnapshot | null>(() => {
  const raw = universeRun.value?.parameters.rotation_return_to_variance_challenger_snapshot
  return isRotationForwardSnapshot(raw) ? raw : null
})

const rotationConcentrationReturnDelta = computed<number | null>(() => {
  const incumbent = universeRotationForward.value?.net_liquidation_return_pct
  const challenger = universeRotationConcentrationChallenger.value?.net_liquidation_return_pct
  if (incumbent === null || incumbent === undefined) return null
  if (challenger === null || challenger === undefined) return null
  return challenger - incumbent
})

const rotationWeightingReturnDelta = computed<number | null>(() => {
  const incumbent = universeRotationForward.value?.net_liquidation_return_pct
  const challenger = universeRotationWeightingChallenger.value?.net_liquidation_return_pct
  if (incumbent === null || incumbent === undefined) return null
  if (challenger === null || challenger === undefined) return null
  return challenger - incumbent
})

const rotationShrinkageReturnDelta = computed<number | null>(() => {
  const incumbent = universeRotationForward.value?.net_liquidation_return_pct
  const challenger = universeRotationShrinkageChallenger.value?.net_liquidation_return_pct
  if (incumbent === null || incumbent === undefined) return null
  if (challenger === null || challenger === undefined) return null
  return challenger - incumbent
})

const rotationReturnToVarianceDelta = computed<number | null>(() => {
  const incumbent = universeRotationForward.value?.net_liquidation_return_pct
  const challenger = universeRotationReturnToVarianceChallenger.value?.net_liquidation_return_pct
  if (incumbent === null || incumbent === undefined) return null
  if (challenger === null || challenger === undefined) return null
  return challenger - incumbent
})

const rotationTrainingWinner = computed<UniverseRotationVariantEvaluation | null>(() => {
  const evaluation = universeRotationEvaluation.value
  if (!evaluation?.selected_variant) return null
  return evaluation.variants.find(
    (variant) => variant.variant.name === evaluation.selected_variant,
  ) ?? null
})

const rotationValidatedChallenger = computed<UniverseRotationVariantEvaluation | null>(() => {
  const evaluation = universeRotationEvaluation.value
  if (!evaluation?.validated_challenger_variant) return null
  return evaluation.variants.find(
    (variant) => variant.variant.name === evaluation.validated_challenger_variant,
  ) ?? null
})

const rotationDisplayVariant = computed<UniverseRotationVariantEvaluation | null>(() => (
  rotationValidatedChallenger.value ?? rotationTrainingWinner.value
))

const rotationScorecardRows = computed<UniverseRotationForwardTrackScore[]>(() => (
  rotationForwardScorecard.value?.tracks ?? []
))

const promotionRows = computed<UniversePromotionReadinessItem[]>(() => {
  if (!promotionReadiness.value) return []
  return [...promotionReadiness.value.items].sort((left, right) => {
    if (left.priority_rank !== right.priority_rank) {
      return left.priority_rank - right.priority_rank
    }
    if (left.priority_score !== right.priority_score) {
      return right.priority_score - left.priority_score
    }
    return left.symbol.localeCompare(right.symbol)
  })
})

const filteredItems = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  const rows = items.value.filter((row) => {
    if (keyword && !`${row.symbol} ${row.alias || ''}`.toLowerCase().includes(keyword)) return false
    if (marketFilter.value && row.market !== marketFilter.value) return false
    if (statusFilter.value === 'active' && !row.is_trading_target) return false
    if (statusFilter.value === 'watching' && row.is_trading_target) return false
    const score = scoreMap.value[row.symbol]
    const review = reviewMap.value[row.symbol]
    if (
      hideStaleScores.value
      && (
        (score !== undefined && isScoreStale(score))
        || (score === undefined && review !== undefined && isScoreStale(review))
      )
    ) return false
    if (scoreBucket.value && quantScoreBucket(score) !== scoreBucket.value) return false
    return true
  })
  return rows.sort((a, b) => {
    if (sortMode.value === 'score_desc') return (scoreMap.value[b.symbol]?.score ?? -1) - (scoreMap.value[a.symbol]?.score ?? -1)
    if (sortMode.value === 'spread_asc') return (spreadFor(a) ?? Number.MAX_SAFE_INTEGER) - (spreadFor(b) ?? Number.MAX_SAFE_INTEGER)
    if (sortMode.value === 'price_desc') return (quoteMap.value[b.symbol]?.last_price ?? -1) - (quoteMap.value[a.symbol]?.last_price ?? -1)
    return a.id - b.id
  })
})

const selectedRows = computed(() => filteredItems.value.filter((row) => selectedIds.value.includes(row.id)))

const allFilteredSelected = computed(() => filteredItems.value.length > 0 && filteredItems.value.every((row) => selectedIds.value.includes(row.id)))

const lastRefreshLabel = computed(() => {
  if (!lastRefreshAt.value) return '未刷新'
  return lastRefreshAt.value.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
})

const scoreBucketLabel = computed(() => {
  if (scoreBucket.value === 'high') return '量化优选'
  if (scoreBucket.value === 'mid') return '量化观察'
  if (scoreBucket.value === 'low') return '量化回避'
  return ''
})

const sortModeLabel = computed(() => {
  if (sortMode.value === 'score_desc') return '评分从高到低'
  if (sortMode.value === 'spread_asc') return '价差从小到大'
  if (sortMode.value === 'price_desc') return '最新价从高到低'
  return ''
})

function openScoreDrawer(score: WatchlistScore) {
  scoreDrawer.score = score
  scoreDrawer.visible = true
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function universeStatusLabel(status: string): string {
  switch (status.toUpperCase()) {
    case 'COMPLETE':
    case 'SUCCEEDED':
    case 'COMPLETED':
      return '已完成'
    case 'DEGRADED':
      return '数据不足'
    case 'RUNNING':
      return '运行中'
    case 'FAILED':
      return '失败'
    default:
      return status
  }
}

function universeStatusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (status.toUpperCase()) {
    case 'COMPLETE':
    case 'SUCCEEDED':
    case 'COMPLETED':
      return 'success'
    case 'DEGRADED':
    case 'RUNNING':
      return 'warning'
    case 'FAILED':
      return 'danger'
    default:
      return 'info'
  }
}

function promotionStatusMeta(status: UniversePromotionForwardStatus): {
  label: string
  type: 'success' | 'warning' | 'danger' | 'info'
} {
  switch (status) {
    case 'FROZEN':
      return { label: '已冻结', type: 'info' }
    case 'COLLECTING':
      return { label: '前向采集中', type: 'warning' }
    case 'READY_FOR_REVIEW':
      return { label: '可人工复核', type: 'success' }
    case 'MATURE_EVIDENCE':
      return { label: '证据成熟', type: 'success' }
    case 'BLOCKED':
      return { label: '已阻塞', type: 'danger' }
    case 'NOT_REGISTERED':
    default:
      return { label: '尚未注册', type: 'info' }
  }
}

function promotionSourceLabel(row: UniversePromotionReadinessItem): string {
  if (row.rank !== null) return `原 #${row.rank}`
  if (row.universe_role === 'EXPLORATION') return '探索池'
  return '池外实盘'
}

function promotionProgressPercent(includedPairs: number, minimumMaturePairs: number): number {
  if (!Number.isFinite(minimumMaturePairs) || minimumMaturePairs <= 0) return 0
  return Math.min(100, Math.max(0, (includedPairs / minimumMaturePairs) * 100))
}

function formatSignedPnl(value: number): string {
  if (!Number.isFinite(value)) return '-'
  const sign = value > 0 ? '+' : value < 0 ? '-' : ''
  return `${sign}$${Math.abs(value).toFixed(2)}`
}

function pnlClass(value: number): string {
  if (value > 0) return 'promotion-pnl-positive'
  if (value < 0) return 'promotion-pnl-negative'
  return ''
}

function formatSignedScore(value: number): string {
  if (!Number.isFinite(value)) return '-'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(1)}`
}

function promotionQuantActionLabel(action: string): string {
  switch (action.toUpperCase()) {
    case 'CANDIDATE': return '优选'
    case 'WATCH': return '观察'
    case 'AVOID': return '回避'
    case 'BUY': return '买入'
    case 'SELL': return '卖出'
    case 'HOLD': return '观望'
    default: return action || '未评分'
  }
}

function promotionQuantState(
  row: UniversePromotionReadinessItem,
): 'MISSING' | 'ERROR' | 'STALE' | 'FRESH' {
  if (!row.quant_source || row.quant_score === null) return 'MISSING'
  if (row.quant_source.startsWith('quant_error')) return 'ERROR'
  return row.quant_fresh ? 'FRESH' : 'STALE'
}

function promotionQuantScoreLabel(row: UniversePromotionReadinessItem): string {
  const state = promotionQuantState(row)
  return state === 'MISSING' || state === 'ERROR'
    ? '-'
    : formatScore(row.quant_score)
}

function promotionQuantOutcomeLabel(row: UniversePromotionReadinessItem): string {
  const state = promotionQuantState(row)
  if (state === 'MISSING') return '未评分'
  if (state === 'ERROR') {
    return `数据异常 · ${formatSignedScore(row.quant_adjustment)}`
  }
  const action = promotionQuantActionLabel(row.quant_recommended_action)
  return state === 'FRESH'
    ? `${action} · ${formatSignedScore(row.quant_adjustment)}`
    : action
}

function promotionQuantTitle(row: UniversePromotionReadinessItem): string {
  const confidence = row.quant_confidence === null
    ? '-'
    : `${(row.quant_confidence * 100).toFixed(0)}%`
  const weight = `${(row.quant_weight * 100).toFixed(1)}%`
  const expiresAt = row.quant_expires_at
    ? formatDateTime(row.quant_expires_at)
    : '-'
  const adjustment = formatSignedScore(row.quant_adjustment)
  return `来源 ${row.quant_source || '-'} · 置信度 ${confidence} · 融合权重 ${weight} · 融合调整 ${adjustment} · 过期 ${expiresAt}`
}

const promotionBlockerLabels: Record<string, string> = {
  BASELINE_REPLAY_MISMATCH: '基线回放不一致',
  EVALUATOR_DEFINITION_MISMATCH: '评估器定义不一致',
  EVIDENCE_DIGEST_MISMATCH: '证据摘要校验失败',
  EVIDENCE_DISPOSITION_INVALID: '证据资格字段无效',
  EVIDENCE_EXCLUSION_INVALID: '排除证据语义无效',
  EVIDENCE_PAYLOAD_INVALID: '证据载荷无效',
  EVIDENCE_TARGET_BOUNDARY_INVALID: '目标会话早于注册边界',
  FORWARD_CANDIDATE_NET_PNL_NON_POSITIVE: '候选前向净收益未转正',
  FORWARD_CANDIDATE_NOT_BETTER_THAN_BASELINE: '候选前向收益未优于基线',
  FORWARD_CANDIDATE_TRADES_INSUFFICIENT: '候选闭合交易样本不足',
  FORWARD_EVALUATION_FAILED: '前向评估失败',
  QUANT_ACTION_NOT_CANDIDATE: '量化适配未达到优选',
  QUANT_SCORE_DATA_ERROR: '量化评分数据异常',
  QUANT_SCORE_MISSING: '缺少当前代量化评分',
  QUANT_SCORE_STALE: '当前代量化评分已过期',
  REGISTRATION_BOUNDARY_INVALID: '注册纳入边界无效',
  REGISTRATION_METADATA_INVALID: '注册元数据无效',
  SHADOW_DISABLED: '影子策略未启用',
  SESSION_LOCAL_FEATURE_DRIFT: '日内特征发生偏移',
  SOURCE_VERSION_SUPERSEDED: '冻结源版本已被替换',
  TARGET_EVIDENCE_NOT_KNOWN_AT_EVALUATION: '评估时目标证据尚不可用',
  TARGET_INPUT_HASH_MISMATCH: '基线与候选输入不一致',
  TARGET_STATE_NOT_FLAT: '会话结束时影子状态仍有持仓',
}

function promotionBlockerLabel(blocker: string): string {
  return promotionBlockerLabels[blocker] ?? blocker.replace(/_/g, ' ')
}

function membershipLabel(membership: string): string {
  if (membership === 'NASDAQ_100') return '纳指 100'
  if (membership === 'DJIA') return '道指'
  return membership
}

const rotationPerformanceFields: (keyof UniverseRotationPerformance)[] = [
  'periods',
  'total_return_pct',
  'annualized_return_pct',
  'annualized_volatility_pct',
  'sharpe',
  'max_drawdown_pct',
  'win_rate_pct',
  'average_turnover_pct',
  'total_cost_pct',
  'average_holdings',
  'qqq_total_return_pct',
  'qqq_annualized_return_pct',
  'qqq_sharpe',
  'qqq_max_drawdown_pct',
  'dia_total_return_pct',
  'dia_annualized_return_pct',
  'dia_sharpe',
  'dia_max_drawdown_pct',
  'excess_annualized_return_vs_qqq_pct',
  'excess_annualized_return_vs_dia_pct',
]

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isRotationPerformance(value: unknown): value is UniverseRotationPerformance {
  if (!isRecord(value)) return false
  return rotationPerformanceFields.every((field) => (
    typeof value[field] === 'number' && Number.isFinite(value[field])
  ))
}

function isRotationVariantConfig(value: unknown): value is UniverseRotationVariantConfig {
  if (!isRecord(value) || typeof value.name !== 'string') return false
  const numericFieldsValid = [
    value.lookback_bars,
    value.skip_bars,
    value.sma_bars,
    value.max_selected,
    value.max_per_risk_group,
  ].every((field) => typeof field === 'number' && Number.isFinite(field))
  const weightingValid = (
    value.weighting === undefined
    || value.weighting === 'equal'
    || value.weighting === 'inverse_volatility'
    || value.weighting === 'equal_inverse_volatility_blend'
  )
  const rankingValid = (
    value.ranking === undefined
    || value.ranking === 'raw_momentum'
    || value.ranking === 'return_to_variance'
  )
  const capValid = (
    value.max_position_weight_pct === undefined
    || (
      typeof value.max_position_weight_pct === 'number'
      && Number.isFinite(value.max_position_weight_pct)
      && value.max_position_weight_pct > 0
      && value.max_position_weight_pct <= 100
    )
  )
  const blendPctValid = (
    value.inverse_volatility_blend_pct === undefined
    || (
      typeof value.inverse_volatility_blend_pct === 'number'
      && Number.isFinite(value.inverse_volatility_blend_pct)
      && value.inverse_volatility_blend_pct >= 0
      && value.inverse_volatility_blend_pct <= 100
    )
  )
  const blendSemanticsValid = (
    value.weighting === 'equal_inverse_volatility_blend'
      ? (
          typeof value.inverse_volatility_blend_pct === 'number'
          && value.inverse_volatility_blend_pct > 0
          && value.inverse_volatility_blend_pct < 100
        )
      : (
          value.inverse_volatility_blend_pct === undefined
          || value.inverse_volatility_blend_pct === 0
        )
  )
  return (
    numericFieldsValid
    && rankingValid
    && weightingValid
    && capValid
    && blendPctValid
    && blendSemanticsValid
  )
}

function isRotationValidationFold(value: unknown): boolean {
  if (!isRecord(value)) return false
  const finiteNumbers = [
    value.fold,
    value.training_periods,
    value.validation_periods,
    value.training_score,
  ]
  return (
    finiteNumbers.every((field) => typeof field === 'number' && Number.isFinite(field))
    && typeof value.training_end_date === 'string'
    && typeof value.validation_start_date === 'string'
    && typeof value.validation_end_date === 'string'
    && typeof value.passed === 'boolean'
    && Array.isArray(value.blockers)
    && value.blockers.every((blocker) => typeof blocker === 'string')
    && isRotationPerformance(value.performance)
  )
}

function isRotationVariantEvaluation(value: unknown): value is UniverseRotationVariantEvaluation {
  if (!isRecord(value)) return false
  const expandingFields = [
    value.expanding_validation_passed,
    value.expanding_validation_blockers,
    value.expanding_folds_passed,
    value.expanding_folds_total,
    value.expanding_validation,
    value.expanding_folds,
  ]
  const hasExpandingFields = expandingFields.some((field) => field !== undefined)
  const expandingFieldsValid = !hasExpandingFields || (
    typeof value.expanding_validation_passed === 'boolean'
    && Array.isArray(value.expanding_validation_blockers)
    && value.expanding_validation_blockers.every((blocker) => typeof blocker === 'string')
    && typeof value.expanding_folds_passed === 'number'
    && Number.isFinite(value.expanding_folds_passed)
    && typeof value.expanding_folds_total === 'number'
    && Number.isFinite(value.expanding_folds_total)
    && isRotationPerformance(value.expanding_validation)
    && Array.isArray(value.expanding_folds)
    && value.expanding_folds.every(isRotationValidationFold)
  )
  return (
    isRotationVariantConfig(value.variant)
    && typeof value.training_score === 'number'
    && Number.isFinite(value.training_score)
    && typeof value.validation_passed === 'boolean'
    && Array.isArray(value.validation_blockers)
    && value.validation_blockers.every((blocker) => typeof blocker === 'string')
    && expandingFieldsValid
    && isRotationPerformance(value.full)
    && isRotationPerformance(value.training)
    && isRotationPerformance(value.validation)
  )
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isNullableFiniteNumber(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isFinite(value))
}

function isRotationForwardHolding(
  value: unknown,
): value is UniverseRotationForwardHolding {
  if (!isRecord(value)) return false
  const rankingValid = (
    value.ranking_method === undefined
    || value.ranking_method === 'raw_momentum'
    || value.ranking_method === 'return_to_variance'
  )
  const formationVolatilityValid = (
    value.formation_realized_volatility === undefined
    || isNullableFiniteNumber(value.formation_realized_volatility)
  )
  const rankingMetricValid = (
    value.ranking_metric === undefined
    || isNullableFiniteNumber(value.ranking_metric)
  )
  return (
    typeof value.symbol === 'string'
    && typeof value.rank === 'number'
    && Number.isFinite(value.rank)
    && typeof value.risk_group === 'string'
    && typeof value.weight_pct === 'number'
    && Number.isFinite(value.weight_pct)
    && typeof value.momentum_pct === 'number'
    && Number.isFinite(value.momentum_pct)
    && rankingValid
    && formationVolatilityValid
    && rankingMetricValid
    && isNullableFiniteNumber(value.entry_price)
    && isNullableFiniteNumber(value.mark_price)
    && isNullableFiniteNumber(value.gross_return_pct)
    && typeof value.signal_spread_bps === 'number'
    && Number.isFinite(value.signal_spread_bps)
    && isNullableFiniteNumber(value.mark_spread_bps)
    && typeof value.data_status === 'string'
  )
}

function isRotationForwardSnapshot(
  value: unknown,
): value is UniverseRotationForwardSnapshot {
  if (!isRecord(value)) return false
  const nullableNumbers = [
    value.gross_return_pct,
    value.entry_cost_pct,
    value.estimated_exit_cost_pct,
    value.total_estimated_cost_pct,
    value.net_liquidation_return_pct,
    value.qqq_return_pct,
    value.dia_return_pct,
    value.excess_return_vs_qqq_pct,
    value.excess_return_vs_dia_pct,
  ]
  return (
    typeof value.algorithm_version === 'string'
    && typeof value.rotation_algorithm_version === 'string'
    && typeof value.status === 'string'
    && typeof value.evidence_mode === 'string'
    && isNullableString(value.cohort_month)
    && typeof value.variant_name === 'string'
    && isNullableString(value.signal_date)
    && isNullableString(value.entry_date)
    && isNullableString(value.mark_date)
    && isNullableString(value.registered_as_of_date)
    && typeof value.forward_eligible === 'boolean'
    && typeof value.selection_drift_detected === 'boolean'
    && Array.isArray(value.target_symbols)
    && value.target_symbols.every((symbol) => typeof symbol === 'string')
    && Array.isArray(value.holdings)
    && value.holdings.every(isRotationForwardHolding)
    && typeof value.elapsed_sessions === 'number'
    && Number.isFinite(value.elapsed_sessions)
    && typeof value.forward_observation_sessions === 'number'
    && Number.isFinite(value.forward_observation_sessions)
    && nullableNumbers.every(isNullableFiniteNumber)
    && typeof value.survivorship_bias === 'boolean'
    && value.order_execution_allowed === false
    && value.automatic_promotion_allowed === false
    && Array.isArray(value.blockers)
    && value.blockers.every((blocker) => typeof blocker === 'string')
  )
}

function isRotationWalkForwardEvaluation(
  value: unknown,
): value is UniverseRotationWalkForwardEvaluation {
  if (!isRecord(value)) return false
  const expandingRootFieldsValid = (
    (
      value.expanding_validation_min_training_periods === undefined
      && value.expanding_validation_fold_periods === undefined
    )
    || (
      typeof value.expanding_validation_min_training_periods === 'number'
      && Number.isFinite(value.expanding_validation_min_training_periods)
      && typeof value.expanding_validation_fold_periods === 'number'
      && Number.isFinite(value.expanding_validation_fold_periods)
    )
  )
  return (
    typeof value.algorithm_version === 'string'
    && typeof value.status === 'string'
    && Array.isArray(value.benchmark_symbols)
    && value.benchmark_symbols.every((symbol) => typeof symbol === 'string')
    && typeof value.data_scope === 'string'
    && typeof value.survivorship_bias === 'boolean'
    && typeof value.validation_periods === 'number'
    && Number.isFinite(value.validation_periods)
    && expandingRootFieldsValid
    && isNullableString(value.selected_variant)
    && typeof value.selected_variant_validation_passed === 'boolean'
    && isNullableString(value.validated_challenger_variant)
    && value.automatic_promotion_allowed === false
    && Array.isArray(value.promotion_blockers)
    && value.promotion_blockers.every((blocker) => typeof blocker === 'string')
    && Array.isArray(value.variants)
    && value.variants.every(isRotationVariantEvaluation)
  )
}

function isMembershipHistorySource(value: unknown): boolean {
  return (
    isRecord(value)
    && typeof value.name === 'string'
    && typeof value.commit === 'string'
    && typeof value.url === 'string'
    && typeof value.license === 'string'
  )
}

function isMembershipHistoryMetadata(
  value: unknown,
): value is UniverseIndexMembershipHistoryMetadata {
  if (!isRecord(value)) return false
  const finiteNumbers = [
    value.catalog_size,
    value.authoritative_symbols,
    value.authoritative_ratio,
  ]
  return (
    typeof value.source_version === 'string'
    && typeof value.effective_start_date === 'string'
    && typeof value.catalog_snapshot_date === 'string'
    && Array.isArray(value.sources)
    && value.sources.every(isMembershipHistorySource)
    && finiteNumbers.every(
      (field) => typeof field === 'number' && Number.isFinite(field),
    )
    && Array.isArray(value.snapshot_only_symbols)
    && value.snapshot_only_symbols.every((symbol) => typeof symbol === 'string')
    && Array.isArray(value.missing_symbols)
    && value.missing_symbols.every((symbol) => typeof symbol === 'string')
  )
}

function isRotationPointInTimeSensitivity(
  value: unknown,
): value is UniverseRotationPointInTimeSensitivity {
  return (
    isRecord(value)
    && typeof value.status === 'string'
    && isMembershipHistoryMetadata(value.membership_history)
    && (
      value.evaluation === null
      || isRotationWalkForwardEvaluation(value.evaluation)
    )
    && Array.isArray(value.errors)
    && value.errors.every((error) => typeof error === 'string')
  )
}

function rotationVariantLabel(value: string | null): string {
  const labels: Record<string, string> = {
    incumbent_top10_12_1: '基线 Top10',
    concentrated_top8_12_1: '集中 Top8',
    concentrated_top6_12_1: '集中 Top6',
    faster_top8_6_1: '快速 Top8',
    diversified_top8_12_1: '分散 Top8',
    diversified_top8_12_1_inverse_vol_25: '波动配权 Top8',
    diversified_top8_12_1_eq75_iv25_cap15: '收缩配权 Top8',
    diversified_top8_12_1_return_to_variance: '收益/方差 Top8',
  }
  return value ? (labels[value] ?? value) : '无'
}

function rotationWalkForwardStatusLabel(value: string): string {
  if (value === 'COMPLETE') return '评估完成'
  if (value === 'HISTORY_INSUFFICIENT') return '样本不足'
  if (value === 'BENCHMARK_DATA_UNAVAILABLE') return '基准不可用'
  if (value === 'EVALUATION_FAILED') return '评估失败'
  return value.replace(/_/g, ' ')
}

function rotationForwardModeLabel(value: string): string {
  if (value === 'FORWARD_PRECOMMITTED') return '前向观察'
  if (value === 'BACKFILLED_AFTER_ENTRY') return '回填观察'
  return '暂不可用'
}

function rotationForwardTagType(
  value: string,
): 'success' | 'warning' | 'info' {
  if (value === 'FORWARD_PRECOMMITTED') return 'success'
  if (value === 'BACKFILLED_AFTER_ENTRY') return 'warning'
  return 'info'
}

function rotationScorecardStatusMeta(
  status: UniverseRotationForwardScorecardStatus,
): {
  label: string
  type: 'success' | 'warning' | 'danger' | 'info'
} {
  switch (status) {
    case 'AWAITING_PRECOMMITMENT':
      return { label: '等待预登记', type: 'info' }
    case 'COLLECTING':
      return { label: '前向采集中', type: 'warning' }
    case 'DATA_BLOCKED':
      return { label: '数据阻塞', type: 'danger' }
    case 'PERFORMANCE_BLOCKED':
      return { label: '表现未达标', type: 'danger' }
    case 'READY_FOR_MANUAL_REVIEW':
      return { label: '可人工复核', type: 'success' }
    case 'NOT_REGISTERED':
    default:
      return { label: '尚未登记', type: 'info' }
  }
}

const rotationScorecardEvidenceLabels: Record<string, string> = {
  FORWARD_COMPLETED_COHORTS_INSUFFICIENT: '完整月样本不足',
  FORWARD_EVIDENCE_INVALID: '证据格式异常',
  FORWARD_SELECTION_DRIFT: '冻结成分发生漂移',
  FORWARD_COHORT_DATA_INCOMPLETE: '前向月份数据不完整',
  FORWARD_COMPOUNDED_RETURN_NON_POSITIVE: '累计收益未转正',
  FORWARD_EXCESS_VS_QQQ_NON_POSITIVE: '累计未跑赢 QQQ',
  FORWARD_EXCESS_VS_DIA_NON_POSITIVE: '累计未跑赢 DIA',
  FORWARD_WIN_RATE_VS_QQQ_INSUFFICIENT: '跑赢 QQQ 的月胜率不足',
  FORWARD_WIN_RATE_VS_DIA_INSUFFICIENT: '跑赢 DIA 的月胜率不足',
  BACKFILLED_COHORTS_EXCLUDED: '回填月份已排除',
  SURVIVORSHIP_BIAS: '当前成分口径含幸存者偏差',
}

function rotationScorecardEvidenceLabel(
  row: UniverseRotationForwardTrackScore,
): string {
  const evidence = [...row.blockers, ...row.warnings]
  if (!evidence.length) return '已满足，可人工复核'
  return evidence
    .map((item) => rotationScorecardEvidenceLabels[item] ?? item.replace(/_/g, ' '))
    .join('；')
}

function rotationScorecardMetricClass(value: number | null): string {
  if (value === null) return ''
  if (value > 0) return 'rotation-scorecard-positive'
  if (value < 0) return 'rotation-scorecard-negative'
  return ''
}

const exclusionReasonLabels: Record<string, string> = {
  DATA_INSUFFICIENT_DAILY_BARS: '日线数据不足',
  DATA_NON_FINITE_DAILY_BAR: '日线含无效值',
  DATA_INVALID_DAILY_BAR: '日线结构异常',
  DATA_INVALID_QUOTE: '买卖盘无效',
  DATA_FETCH_FAILED: '行情获取失败',
  DATA_QUOTE_MISSING: '报价缺失',
  DATA_NO_COMPLETED_DAILY_BAR: '无完整日线',
  DATA_STALE_SESSION_DATE: '交易日数据陈旧',
  PRICE_BELOW_MINIMUM: '股价低于门槛',
  DOLLAR_VOLUME_BELOW_MINIMUM: '流动性不足',
  SPREAD_ABOVE_MAXIMUM: '成本代理过高',
  REALIZED_VOL_OUTSIDE_RANGE: '波动率超出区间',
  ATR_OUTSIDE_RANGE: 'ATR 超出区间',
  SECTOR_CAP: '行业名额已满',
  BELOW_SELECTION_CUTOFF: '综合排序未入围',
}

function exclusionReasonLabel(reason: string): string {
  return exclusionReasonLabels[reason] ?? reason.replace(/_/g, ' ')
}

function formatCoverage(value: number): string {
  if (!Number.isFinite(value)) return '-'
  return `${(Math.min(1, Math.max(0, value)) * 100).toFixed(1)}%`
}

function formatScore(value: number | null | undefined): string {
  return value !== null && value !== undefined && Number.isFinite(value)
    ? value.toFixed(1)
    : '-'
}

function formatDollarVolume(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '-'
  if (value >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(2)}B`
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`
  return `$${value.toFixed(0)}`
}

function formatBps(value: number | null): string {
  return value !== null && Number.isFinite(value) ? `${value.toFixed(1)} bp` : '-'
}

function formatVolatility(value: number | null): string {
  return value !== null && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '-'
}

function formatAtr(value: number | null): string {
  return value !== null && Number.isFinite(value) ? `${value.toFixed(2)}%` : '-'
}

function formatSignedPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(1)}%`
}

function formatPercent(value: number | null | undefined): string {
  return value !== null && value !== undefined && Number.isFinite(value)
    ? `${value.toFixed(1)}%`
    : '-'
}

function formatDecimal(value: number | null | undefined): string {
  return value !== null && value !== undefined && Number.isFinite(value)
    ? value.toFixed(2)
    : '-'
}

function isQuantScore(score: WatchlistScore): boolean {
  return score.source.startsWith('quant_')
}

function isScoreStale(score: WatchlistScore): boolean {
  if (score.is_stale) return true
  const expiresAtMs = Date.parse(score.expires_at)
  return Number.isFinite(expiresAtMs) && expiresAtMs <= scoreClockMs.value
}

function quantScoreBucket(
  score: WatchlistScore | undefined,
): 'high' | 'mid' | 'low' | null {
  if (!score || !isQuantScore(score)) return null
  switch (score.recommended_action) {
    case 'CANDIDATE': return 'high'
    case 'WATCH': return 'mid'
    case 'AVOID': return 'low'
    default: return null
  }
}

function scoreTagType(
  score: WatchlistScore,
): 'success' | 'warning' | 'info' | 'danger' {
  if (score.source.startsWith('fallback')) return 'info'
  if (isQuantScore(score)) {
    if (score.recommended_action === 'CANDIDATE') return 'success'
    if (score.recommended_action === 'WATCH') return 'warning'
    return score.source.startsWith('quant_error') ? 'danger' : 'info'
  }
  if (score.recommended_action === 'BUY') return 'success'
  if (score.recommended_action === 'SELL') return 'warning'
  if (score.recommended_action === 'AVOID') return 'danger'
  return 'info'
}

function scoreVisualClass(score: WatchlistScore): string {
  const type = scoreTagType(score)
  if (type === 'success') return 'score-high'
  if (type === 'warning') return 'score-mid'
  if (type === 'danger') return 'score-low'
  return 'score-none'
}

function scoreActionLabel(action: string): string {
  switch (action) {
    case 'CANDIDATE': return '优选'
    case 'WATCH': return '观察'
    case 'BUY': return '买入'
    case 'SELL': return '卖出'
    case 'AVOID': return '回避'
    case 'HOLD':
    default:
      return '观望'
  }
}

function scoreOutcomeLabel(score: WatchlistScore): string {
  if (score.source.startsWith('fallback')) return '中性兜底'
  if (score.source.startsWith('quant_error')) return '数据异常'
  return scoreActionLabel(score.recommended_action)
}

function scoreSourceLabel(source: string): string {
  if (source === 'quant_v1') return '量化 v1'
  if (source === 'quant_v2') return '量化 v2'
  if (source === 'quant_v3') return '量化 v3'
  if (source === 'quant_v4') return '量化 v4'
  if (source === 'quant_v5') return '量化 v5'
  if (source.startsWith('quant_error')) return '数据异常'
  if (source === 'llm') return 'AI 复核'
  if (source.startsWith('fallback')) return 'AI 降级结果'
  return source
}

function scoreSourceTagType(
  source: string,
): 'success' | 'warning' | 'info' {
  if (source.startsWith('quant_error')) return 'warning'
  if (source.startsWith('fallback')) return 'info'
  return 'success'
}

function watchlistSourceLabel(source: string): string {
  return source === 'universe' ? '指数候选池' : '手动添加'
}

async function loadUniverse() {
  if (universeRefreshing.value) return
  const generation = ++universeRequestGeneration
  universeLoading.value = true
  universeError.value = ''
  const errors: string[] = []
  try {
    const [catalogResult, latestResult] = await Promise.allSettled([
      getUniverseCatalog(),
      getLatestUniverseSelection(),
    ])
    if (generation !== universeRequestGeneration) return
    if (catalogResult.status === 'fulfilled') {
      universeCatalog.value = catalogResult.value
    } else {
      errors.push(resolveErrorMessage(catalogResult.reason, '加载候选目录失败'))
    }

    if (latestResult.status === 'fulfilled') {
      universeRun.value = latestResult.value
    } else if (isAxiosError(latestResult.reason) && latestResult.reason.response?.status === 404) {
      universeRun.value = null
    } else {
      errors.push(resolveErrorMessage(latestResult.reason, '加载候选池结果失败'))
    }
    universeError.value = errors.join('；')
  } finally {
    if (generation === universeRequestGeneration) {
      universeLoading.value = false
    }
  }
}

async function loadPromotionReadiness() {
  const generation = ++promotionRequestGeneration
  promotionLoading.value = true
  promotionError.value = ''
  try {
    const response = await getUniversePromotionReadiness()
    if (generation !== promotionRequestGeneration) return
    promotionReadiness.value = response
  } catch (e: unknown) {
    if (generation !== promotionRequestGeneration) return
    if (isAxiosError(e) && e.response?.status === 404) {
      promotionReadiness.value = null
    } else {
      promotionError.value = resolveErrorMessage(e, '加载前瞻证据失败')
    }
  } finally {
    if (generation === promotionRequestGeneration) {
      promotionLoading.value = false
    }
  }
}

async function loadRotationForwardScorecard() {
  const generation = ++rotationScorecardRequestGeneration
  rotationScorecardLoading.value = true
  rotationScorecardError.value = ''
  try {
    const response = await getRotationForwardScorecard()
    if (generation !== rotationScorecardRequestGeneration) return
    rotationForwardScorecard.value = response
  } catch (e: unknown) {
    if (generation !== rotationScorecardRequestGeneration) return
    if (isAxiosError(e) && e.response?.status === 404) {
      rotationForwardScorecard.value = null
    } else {
      rotationScorecardError.value = resolveErrorMessage(
        e,
        '加载跨月前向记分牌失败',
      )
    }
  } finally {
    if (generation === rotationScorecardRequestGeneration) {
      rotationScorecardLoading.value = false
    }
  }
}

async function handleUniverseRefresh() {
  const generation = ++universeRequestGeneration
  universeRefreshing.value = true
  universeLoading.value = false
  universeError.value = ''
  try {
    const response = await refreshUniverseSelection()
    if (generation !== universeRequestGeneration) return
    universeRun.value = response.run
    void loadPromotionReadiness()
    void loadRotationForwardScorecard()
    if (response.applied) {
      await loadItems()
      await loadQuotes()
    }
    const summary = `候选池已刷新：正式入选 ${response.run.selected_count} 个，探索观察 ${response.exploration_symbols.length} 个，覆盖率 ${formatCoverage(response.run.coverage_ratio)}`
    if (response.run.status.toUpperCase() !== 'COMPLETE') {
      ElMessage.warning(`${summary}；${response.run.error || response.reason || '数据覆盖不足'}`)
    } else if (response.shadow_failed_symbols.length > 0) {
      ElMessage.warning(
        `${summary}；部分 Shadow 同步失败：${response.shadow_failed_symbols.join('、')}`,
      )
    } else {
      ElMessage.success(summary)
    }
  } catch (e: unknown) {
    if (generation !== universeRequestGeneration) return
    universeError.value = resolveErrorMessage(e, '刷新候选池失败')
    ElMessage.error(universeError.value)
  } finally {
    if (generation === universeRequestGeneration) {
      universeRefreshing.value = false
    }
  }
}

async function loadItems() {
  loading.value = true
  try {
    items.value = await getWatchlist()
    const ids = new Set(items.value.map((item) => item.id))
    selectedIds.value = selectedIds.value.filter((id) => ids.has(id))
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '加载观察列表失败'))
  } finally {
    loading.value = false
  }
}

async function loadQuotes() {
  if (items.value.length === 0) return
  try {
    const quotes = await getWatchlistQuotes()
    const map: Record<string, WatchlistQuote> = {}
    for (const q of quotes) {
      map[q.symbol] = q
    }
    quoteMap.value = map
    lastRefreshAt.value = new Date()
    quoteFailureStreak = 0
  } catch (e: unknown) {
    quoteFailureStreak += 1
    // Throttle the user-visible toast: only show it for the first 3 consecutive
    // failures, then suppress further toasts for a cooldown window. The streak
    // counter resets on the next successful fetch.
    const now = Date.now()
    const inCooldown = now - lastQuoteFailureToastAt < QUOTE_FAILURE_TOAST_COOLDOWN_MS
    if (quoteFailureStreak <= QUOTE_FAILURE_TOAST_THRESHOLD && !inCooldown) {
      ElMessage.error(resolveErrorMessage(e, '加载行情失败'))
      lastQuoteFailureToastAt = now
    }
    if (quoteFailureStreak === QUOTE_FAILURE_TOAST_THRESHOLD) {
      // One final warning so the user knows the silent mode has kicked in.
      ElMessage.warning('行情连续加载失败，已暂停错误提示，下次成功后将恢复')
    }
  }
}

function clearFilters() {
  searchText.value = ''
  marketFilter.value = ''
  statusFilter.value = ''
  scoreBucket.value = ''
  hideStaleScores.value = false
  sortMode.value = 'default'
}

function toggleSelection(id: number) {
  selectedIds.value = selectedIds.value.includes(id)
    ? selectedIds.value.filter((value) => value !== id)
    : [...selectedIds.value, id]
}

function toggleSelectAll(value: string | number | boolean) {
  if (Boolean(value)) {
    selectedIds.value = Array.from(new Set([...selectedIds.value, ...filteredItems.value.map((row) => row.id)]))
  } else {
    const filtered = new Set(filteredItems.value.map((row) => row.id))
    selectedIds.value = selectedIds.value.filter((id) => !filtered.has(id))
  }
}

async function copySymbol(symbol: string) {
  try {
    await navigator.clipboard.writeText(symbol)
    ElMessage.success(`已复制 ${symbol}`)
  } catch {
    ElMessage.error('复制失败')
  }
}

function rowsToCsv(rows: WatchlistItem[]) {
  return rows.map((row) => {
    const q = quoteMap.value[row.symbol]
    const quant = scoreMap.value[row.symbol]
    const review = reviewMap.value[row.symbol]
    return {
      symbol: row.symbol,
      market: row.market,
      alias: row.alias || '',
      last_price: q?.last_price ?? '',
      quant_score: quant?.score ?? '',
      quant_action: quant ? scoreOutcomeLabel(quant) : '',
      quant_source: quant ? scoreSourceLabel(quant.source) : '',
      review_score: review?.score ?? '',
      review_action: review ? scoreOutcomeLabel(review) : '',
      review_source: review ? scoreSourceLabel(review.source) : '',
      is_trading_target: row.is_trading_target ? 'yes' : 'no',
    }
  })
}

function exportSelected() {
  downloadCsv('watchlist_selected.csv', [
    { key: 'symbol', label: 'symbol' },
    { key: 'market', label: 'market' },
    { key: 'alias', label: 'alias' },
    { key: 'last_price', label: 'last_price' },
    { key: 'quant_score', label: 'quant_score' },
    { key: 'quant_action', label: 'quant_action' },
    { key: 'quant_source', label: 'quant_source' },
    { key: 'review_score', label: 'review_score' },
    { key: 'review_action', label: 'review_action' },
    { key: 'review_source', label: 'review_source' },
    { key: 'is_trading_target', label: 'is_trading_target' },
  ], rowsToCsv(selectedRows.value))
  ElMessage.success(`已导出 ${selectedRows.value.length} 个标的`)
}

async function confirmBulkDelete() {
  const ids = selectedRows.value.map((row) => row.id)
  bulkDeleteDialog.value = false
  const results = await Promise.allSettled(ids.map((id) => removeWatchlistItem(id)))
  const failed = results.filter((result) => result.status === 'rejected').length
  selectedIds.value = selectedIds.value.filter((id) => !ids.includes(id))
  try {
    await loadItems()
    await loadQuotes()
  } finally {
    if (failed > 0) {
      ElMessage.error(`批量删除完成：成功 ${ids.length - failed}，失败 ${failed}`)
    } else {
      ElMessage.success(`已删除 ${ids.length} 个标的`)
    }
  }
}

async function refreshNow() {
  await loadQuotes()
  if (!quantRanking.value && scoringSymbol.value === null) {
    await loadScores()
  }
}

async function handleAdd() {
  if (!newSymbol.value.trim()) return
  adding.value = true
  addError.value = ''
  try {
    await addWatchlistItem({
      symbol: newSymbol.value.trim().toUpperCase(),
      market: newMarket.value,
      alias: newAlias.value.trim(),
    })
    newSymbol.value = ''
    newAlias.value = ''
    await loadItems()
    await loadQuotes()
  } catch (e: unknown) {
    addError.value = resolveErrorMessage(e, '添加失败')
  } finally {
    adding.value = false
  }
}

async function handleRemove(id: number) {
  removingId.value = id
  try {
    await removeWatchlistItem(id)
    await loadItems()
    await loadQuotes()
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '删除失败'))
  } finally {
    removingId.value = null
  }
}

async function handleActivate(id: number) {
  activatingId.value = id
  try {
    await activateWatchlistItem(id)
    await loadItems()
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '激活失败'))
  } finally {
    activatingId.value = null
  }
}

async function loadScores() {
  if (quantRanking.value || scoringSymbol.value !== null) return
  const quantGeneration = ++quantScoreGeneration
  const reviewGeneration = ++reviewScoreGeneration
  try {
    const response = await getWatchlistScores()
    if (quantGeneration === quantScoreGeneration) {
      const quantMap: Record<string, WatchlistScore> = {}
      for (const row of response.scores) {
        if (isQuantScore(row)) quantMap[row.symbol] = row
      }
      scoreMap.value = quantMap
    }
    if (reviewGeneration === reviewScoreGeneration) {
      const latestReviews: Record<string, WatchlistScore> = {}
      for (const row of response.reviews) {
        if (!isQuantScore(row)) latestReviews[row.symbol] = row
      }
      reviewMap.value = latestReviews
    }
  } catch (e: unknown) {
    if (
      quantGeneration === quantScoreGeneration
      || reviewGeneration === reviewScoreGeneration
    ) {
      ElMessage.warning(resolveErrorMessage(e, '加载候选评估失败'))
    }
  }
}

async function handleQuantRank() {
  const generation = ++quantScoreGeneration
  quantRanking.value = true
  try {
    const scores = await rankWatchlistQuant(360)
    if (generation !== quantScoreGeneration) return
    const map: Record<string, WatchlistScore> = {}
    for (const score of scores) {
      if (isQuantScore(score)) map[score.symbol] = score
    }
    scoreMap.value = map
    const preferred = scores.filter(
      (score) => score.recommended_action === 'CANDIDATE',
    ).length
    ElMessage.success(
      `当前量化快照：${scores.length} 个标的，优选 ${preferred} 个；闭市市场保留最近结果`,
    )
  } catch (e: unknown) {
    if (generation === quantScoreGeneration) {
      ElMessage.error(resolveErrorMessage(e, '量化评分失败'))
    }
  } finally {
    if (generation === quantScoreGeneration) {
      quantRanking.value = false
    }
  }
}

async function handleScore(symbol: string, market: 'US' | 'HK') {
  if (scoringSymbol.value !== null) return
  const generation = ++reviewScoreGeneration
  scoringSymbol.value = symbol
  try {
    const score = await scoreWatchlistSymbol({ symbol, market, ttl_minutes: 60 })
    if (generation !== reviewScoreGeneration) return
    reviewMap.value = { ...reviewMap.value, [symbol]: score }
    ElMessage.success(
      `${symbol} AI 复核 ${score.score.toFixed(0)}（${scoreOutcomeLabel(score)}）`,
    )
  } catch (e: unknown) {
    if (generation === reviewScoreGeneration) {
      ElMessage.error(resolveErrorMessage(e, 'AI 复核请求失败'))
    }
  } finally {
    if (generation === reviewScoreGeneration) {
      scoringSymbol.value = null
    }
  }
}

/** ask - bid for a row, or null when quotes are missing/invalid. */
function spreadFor(row: WatchlistItem): number | null {
  const q = quoteMap.value[row.symbol]
  if (!q || q.ask == null || q.bid == null) return null
  const spread = q.ask - q.bid
  return Number.isFinite(spread) ? spread : null
}

function exportSnapshot() {
  const rows = items.value.map((row) => {
    const q = quoteMap.value[row.symbol]
    const quant = scoreMap.value[row.symbol]
    const review = reviewMap.value[row.symbol]
    return {
      symbol: row.symbol,
      market: row.market,
      alias: row.alias || '',
      last_price: q?.last_price ?? '',
      bid: q?.bid ?? '',
      ask: q?.ask ?? '',
      spread: spreadFor(row) ?? '',
      quant_score: quant?.score ?? '',
      quant_action: quant ? scoreOutcomeLabel(quant) : '',
      quant_source: quant ? scoreSourceLabel(quant.source) : '',
      quant_confidence: quant?.confidence ?? '',
      quant_stale: quant ? (isScoreStale(quant) ? 'yes' : 'no') : '',
      review_score: review?.score ?? '',
      review_action: review ? scoreOutcomeLabel(review) : '',
      review_source: review ? scoreSourceLabel(review.source) : '',
      review_confidence: review?.confidence ?? '',
      review_stale: review ? (isScoreStale(review) ? 'yes' : 'no') : '',
      is_trading_target: row.is_trading_target ? 'yes' : 'no',
    }
  })
  downloadCsv('watchlist_snapshot.csv', [
    { key: 'symbol', label: 'symbol' },
    { key: 'market', label: 'market' },
    { key: 'alias', label: 'alias' },
    { key: 'last_price', label: 'last_price' },
    { key: 'bid', label: 'bid' },
    { key: 'ask', label: 'ask' },
    { key: 'spread', label: 'spread' },
    { key: 'quant_score', label: 'quant_score' },
    { key: 'quant_action', label: 'quant_action' },
    { key: 'quant_source', label: 'quant_source' },
    { key: 'quant_confidence', label: 'quant_confidence' },
    { key: 'quant_stale', label: 'quant_stale' },
    { key: 'review_score', label: 'review_score' },
    { key: 'review_action', label: 'review_action' },
    { key: 'review_source', label: 'review_source' },
    { key: 'review_confidence', label: 'review_confidence' },
    { key: 'review_stale', label: 'review_stale' },
    { key: 'is_trading_target', label: 'is_trading_target' },
  ], rows)
  ElMessage.success('已导出观察列表快照')
}

useRegisterViewRefresh(() => {
  void Promise.all([
    loadItems(),
    loadUniverse(),
    loadPromotionReadiness(),
    loadRotationForwardScorecard(),
  ])
})

onMounted(() => {
  scoreClockMs.value = Date.now()
  void Promise.all([
    loadUniverse(),
    loadPromotionReadiness(),
    loadRotationForwardScorecard(),
  ])
  loadItems().then(() => {
    loadQuotes()
    loadScores()
  })
  quoteTimer = setInterval(loadQuotes, 15000)
  scoreExpiryTimer = setInterval(() => {
    scoreClockMs.value = Date.now()
  }, 60_000)
})

watch([searchText, marketFilter, statusFilter, scoreBucket, hideStaleScores], () => {
  const visible = new Set(filteredItems.value.map((row) => row.id))
  selectedIds.value = selectedIds.value.filter((id) => visible.has(id))
})

onUnmounted(() => {
  universeRequestGeneration += 1
  rotationScorecardRequestGeneration += 1
  promotionRequestGeneration += 1
  quantScoreGeneration += 1
  reviewScoreGeneration += 1
  if (quoteTimer) clearInterval(quoteTimer)
  if (scoreExpiryTimer) clearInterval(scoreExpiryTimer)
})
</script>

<style scoped>
.watchlist-page {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  max-width: 900px;
  margin: 0 auto;
  padding: 16px;
  overflow-x: hidden;
}

.page-heading {
  margin-bottom: 16px;
}

.page-heading h3 {
  margin: 0 0 4px;
}

.page-heading p {
  margin: 0;
  color: #909399;
  font-size: 13px;
}

.universe-panel {
  margin-bottom: 20px;
}

.universe-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.universe-heading {
  min-width: 0;
}

.universe-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.universe-heading p {
  margin: 5px 0 0;
  color: #7a8089;
  font-size: 12px;
  line-height: 1.5;
}

.universe-content {
  min-height: 72px;
}

.universe-alert {
  margin-bottom: 12px;
}

.universe-summary {
  display: grid;
  grid-template-columns: repeat(8, minmax(76px, 1fr));
  gap: 1px;
  margin-bottom: 14px;
  overflow: hidden;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #ebeef5;
}

.universe-summary-item {
  display: flex;
  min-width: 0;
  min-height: 58px;
  padding: 9px 10px;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  background: var(--el-bg-color);
}

.universe-summary-item span {
  color: #909399;
  font-size: 11px;
}

.universe-summary-item strong {
  overflow: hidden;
  color: #303133;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rotation-forward {
  min-width: 0;
  margin-bottom: 14px;
  padding: 12px 0;
  border-top: 1px solid var(--el-border-color);
  border-bottom: 1px solid var(--el-border-color);
}

.rotation-forward-header {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.rotation-forward-title {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.rotation-forward-header small {
  display: block;
  overflow: hidden;
  max-width: 360px;
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rotation-forward-dates {
  display: flex;
  flex-shrink: 0;
  gap: 14px;
}

.rotation-forward-dates span {
  display: flex;
  align-items: flex-end;
  flex-direction: column;
  gap: 2px;
  color: var(--el-text-color-secondary);
  font-size: 10px;
}

.rotation-forward-dates strong {
  color: var(--el-text-color-primary);
  font-size: 12px;
}

.rotation-forward-symbols {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 10px;
  margin-top: 11px;
}

.rotation-forward-symbols > span {
  flex-shrink: 0;
  padding-top: 4px;
  color: var(--el-text-color-regular);
  font-size: 11px;
}

.rotation-forward-symbols > div {
  display: flex;
  min-width: 0;
  gap: 5px;
  flex-wrap: wrap;
}

.rotation-forward-symbols > strong {
  padding-top: 3px;
  color: var(--el-text-color-regular);
  font-size: 12px;
}

.rotation-forward-metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 1px;
  margin-top: 11px;
  overflow: hidden;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-border-color-lighter);
}

.rotation-forward-metrics div {
  display: flex;
  min-width: 0;
  min-height: 50px;
  padding: 8px;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  background: var(--el-bg-color);
}

.rotation-forward-metrics span {
  color: var(--el-text-color-secondary);
  font-size: 10px;
}

.rotation-forward-metrics strong {
  overflow: hidden;
  color: var(--el-text-color-primary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rotation-forward-footer {
  display: flex;
  min-width: 0;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 10px;
  line-height: 1.5;
}

.rotation-forward-footer strong {
  color: var(--el-color-warning-dark-2);
  font-weight: 500;
  text-align: right;
}

.rotation-scorecard {
  min-width: 0;
  margin-bottom: 14px;
  padding: 12px 0;
  border-top: 1px solid var(--el-border-color);
  border-bottom: 1px solid var(--el-border-color);
}

.rotation-scorecard-header {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.rotation-scorecard-title {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.rotation-scorecard-header p {
  margin: 5px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 11px;
  line-height: 1.5;
}

.rotation-scorecard-meta {
  display: flex;
  flex-shrink: 0;
  align-items: flex-end;
  flex-direction: column;
  gap: 2px;
  color: var(--el-text-color-secondary);
  font-size: 10px;
}

.rotation-scorecard-alert {
  margin-top: 10px;
}

.rotation-scorecard-content {
  min-height: 24px;
  margin-top: 10px;
}

.rotation-scorecard-table-view {
  display: block;
  overflow: hidden;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}

.rotation-scorecard-mobile-list {
  display: none;
}

.rotation-scorecard-variant,
.rotation-scorecard-pair {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}

.rotation-scorecard-variant strong {
  overflow: hidden;
  color: var(--el-text-color-primary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rotation-scorecard-variant small,
.rotation-scorecard-pair span {
  color: var(--el-text-color-secondary);
  font-size: 10px;
  line-height: 1.4;
}

.rotation-scorecard-blockers {
  display: block;
  color: var(--el-color-warning-dark-2);
  font-size: 10px;
  line-height: 1.45;
  word-break: break-word;
}

.rotation-scorecard-clear,
.rotation-scorecard-positive {
  color: var(--el-color-success-dark-2);
}

.rotation-scorecard-negative {
  color: var(--el-color-danger);
}

.rotation-scorecard-note {
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 10px;
  line-height: 1.5;
}

.rotation-evaluation {
  min-width: 0;
  margin-bottom: 14px;
  padding: 12px 0;
  border-top: 1px solid #dcdfe6;
  border-bottom: 1px solid #dcdfe6;
}

.rotation-evaluation-header {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.rotation-evaluation-title {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.rotation-evaluation-header small {
  display: block;
  overflow: hidden;
  max-width: 360px;
  margin-top: 4px;
  color: #909399;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rotation-evaluation-selection {
  display: flex;
  flex-shrink: 0;
  gap: 14px;
}

.rotation-evaluation-selection span {
  display: flex;
  align-items: flex-end;
  flex-direction: column;
  gap: 2px;
  color: #909399;
  font-size: 10px;
}

.rotation-evaluation-selection strong {
  color: #303133;
  font-size: 12px;
}

.rotation-evaluation-metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 1px;
  margin-top: 11px;
  overflow: hidden;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #ebeef5;
}

.rotation-evaluation-metrics div {
  display: flex;
  min-width: 0;
  min-height: 50px;
  padding: 8px;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  background: var(--el-bg-color);
}

.rotation-evaluation-metrics span {
  color: #909399;
  font-size: 10px;
}

.rotation-evaluation-metrics strong {
  overflow: hidden;
  color: #303133;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rotation-evaluation-footer {
  display: flex;
  min-width: 0;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  color: #909399;
  font-size: 10px;
  line-height: 1.5;
}

.rotation-evaluation-footer strong {
  color: #b26a00;
  font-weight: 500;
  text-align: right;
}

.rotation-point-in-time {
  border-top: 0;
}

.rotation-point-in-time-variants {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 1px;
  margin-top: 11px;
  overflow: hidden;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #ebeef5;
}

.rotation-point-in-time-variant {
  display: grid;
  min-width: 0;
  min-height: 54px;
  grid-template-columns: minmax(150px, 1.4fr) repeat(4, minmax(74px, 1fr));
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  background: var(--el-bg-color);
}

.rotation-point-in-time-variant-title,
.rotation-point-in-time-variant > span {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}

.rotation-point-in-time-variant-title strong,
.rotation-point-in-time-variant > span strong {
  overflow: hidden;
  color: #303133;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rotation-point-in-time-variant-title small,
.rotation-point-in-time-variant > span {
  color: #909399;
  font-size: 10px;
}

.universe-symbol {
  display: flex;
  min-width: 0;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
}

.universe-symbol small,
.universe-score {
  display: block;
  color: #909399;
  font-size: 11px;
}

.universe-rank {
  display: flex;
  min-height: 42px;
  flex-direction: column;
  justify-content: center;
  gap: 3px;
}

.universe-rank > span {
  display: block;
}

.universe-risk-metrics {
  display: flex;
  min-height: 54px;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
  line-height: 1.25;
}

.universe-risk-metrics span {
  white-space: nowrap;
}

.universe-risk-metrics small {
  margin-right: 5px;
  color: #909399;
  font-size: 10px;
}

.universe-memberships,
.universe-state-tags,
.universe-reasons {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.universe-state-tags {
  align-items: flex-start;
  flex-direction: column;
}

.universe-pass {
  color: #14884f;
  font-size: 12px;
}

.universe-table-view {
  max-width: 100%;
  overflow: hidden;
}

.universe-mobile-list {
  display: none;
}

.universe-footer {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
  color: #909399;
  font-size: 11px;
  line-height: 1.5;
}

.promotion-readiness {
  min-width: 0;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid #dcdfe6;
}

.promotion-header {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.promotion-heading {
  min-width: 0;
}

.promotion-title,
.promotion-badges,
.promotion-mobile-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.promotion-heading p {
  margin: 5px 0 0;
  color: #7a8089;
  font-size: 12px;
  line-height: 1.5;
}

.promotion-meta {
  display: flex;
  flex-shrink: 0;
  align-items: flex-end;
  flex-direction: column;
  gap: 2px;
  color: #909399;
  font-size: 10px;
  line-height: 1.4;
}

.promotion-alert {
  margin-bottom: 12px;
}

.promotion-content {
  min-height: 64px;
}

.promotion-table-view {
  max-width: 100%;
  overflow: hidden;
}

.promotion-mobile-list {
  display: none;
}

.promotion-symbol,
.promotion-quant,
.promotion-progress,
.promotion-comparison {
  display: flex;
  min-width: 0;
  flex-direction: column;
}

.promotion-symbol {
  align-items: flex-start;
  gap: 5px;
}

.promotion-secondary {
  display: block;
  color: #909399;
  font-size: 10px;
}

.promotion-quant {
  align-items: flex-start;
  gap: 4px;
}

.promotion-quant > div {
  display: flex;
  align-items: baseline;
  gap: 5px;
}

.promotion-quant span,
.promotion-progress span,
.promotion-progress small,
.promotion-comparison span {
  color: #909399;
  font-size: 10px;
}

.promotion-progress {
  gap: 4px;
}

.promotion-progress > div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 5px;
}

.promotion-comparison {
  align-items: flex-end;
  gap: 3px;
}

.promotion-comparison strong {
  color: #303133;
  font-size: 11px;
}

.promotion-pnl-positive {
  color: #14884f !important;
}

.promotion-pnl-negative {
  color: #c45656 !important;
}

.promotion-blockers {
  color: #c45656;
  font-size: 11px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.promotion-clear {
  color: #14884f;
  font-size: 11px;
}

.promotion-manual-note {
  margin-top: 10px;
  padding: 7px 9px;
  border-left: 3px solid #e6a23c;
  background: #fdf6ec;
  color: #6b5a38;
  font-size: 11px;
  line-height: 1.5;
}

.score-tag {
  cursor: pointer;
}

.score-stack {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 6px;
}

.score-channel {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.score-channel-label {
  width: 52px;
  flex: 0 0 52px;
  color: #606266;
  font-size: 11px;
}

.score-channel-empty,
.score-source,
.score-outcome {
  color: #909399;
  font-size: 10px;
}

.score-source {
  padding-left: 5px;
  border-left: 1px solid #dcdfe6;
}

.watchlist-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.watchlist-toolbar-note {
  color: #909399;
  font-size: 12px;
}

.watchlist-source {
  color: #909399;
  font-size: 10px;
}

.watchlist-table-scroll {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
}

.watchlist-filters,
.watchlist-bulk-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.watchlist-filter-summary {
  margin-bottom: 10px;
  color: #606266;
  font-size: 12px;
}

.bulk-delete-symbols {
  color: #606266;
  font-size: 12px;
  word-break: break-all;
}

.score-detail-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 20px;
}

.score-detail-score {
  font-size: 48px;
  font-weight: 800;
  line-height: 1;
}

.score-high {
  color: #14884f;
}

.score-mid {
  color: #e6a23c;
}

.score-low {
  color: #409eff;
}

.score-none {
  color: #909399;
}

.score-detail-section {
  margin-bottom: 16px;
}

.score-detail-label {
  margin-bottom: 6px;
  color: #909399;
  font-size: 12px;
}

.score-detail-rationale {
  margin: 0;
  color: #4b5563;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

@media (max-width: 768px) {
  .watchlist-page {
    padding: 10px;
  }

  .universe-panel :deep(.el-card__header),
  .universe-panel :deep(.el-card__body) {
    padding: 12px;
  }

  .universe-header {
    align-items: stretch;
    flex-direction: column;
  }

  .universe-header .el-button {
    width: 100%;
    margin: 0;
  }

  .universe-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .rotation-forward-header,
  .rotation-forward-footer {
    flex-direction: column;
    gap: 7px;
  }

  .rotation-forward-dates {
    width: 100%;
    justify-content: space-between;
    gap: 6px;
  }

  .rotation-forward-dates span:first-child {
    align-items: flex-start;
  }

  .rotation-forward-dates span:last-child {
    align-items: flex-end;
  }

  .rotation-forward-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .rotation-forward-metrics div:last-child {
    grid-column: 1 / -1;
  }

  .rotation-forward-footer strong {
    text-align: left;
  }

  .rotation-scorecard-header {
    flex-direction: column;
    gap: 7px;
  }

  .rotation-scorecard-meta {
    width: 100%;
    align-items: flex-start;
    flex-direction: row;
    gap: 8px;
    flex-wrap: wrap;
  }

  .rotation-scorecard-table-view {
    display: none;
  }

  .rotation-scorecard-mobile-list {
    display: block;
    border-top: 1px solid var(--el-border-color-lighter);
  }

  .rotation-scorecard-mobile-row {
    padding: 11px 0;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }

  .rotation-scorecard-mobile-row:last-child {
    border-bottom: 0;
  }

  .rotation-scorecard-mobile-heading {
    display: flex;
    min-width: 0;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
  }

  .rotation-scorecard-mobile-heading > div {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 3px;
  }

  .rotation-scorecard-mobile-heading small {
    color: var(--el-text-color-secondary);
    font-size: 10px;
  }

  .rotation-scorecard-mobile-metrics {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1px;
    margin-top: 9px;
    overflow: hidden;
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 4px;
    background: var(--el-border-color-lighter);
  }

  .rotation-scorecard-mobile-metrics > div {
    display: flex;
    min-width: 0;
    min-height: 45px;
    padding: 7px;
    flex-direction: column;
    justify-content: center;
    gap: 3px;
    background: var(--el-bg-color);
  }

  .rotation-scorecard-mobile-metrics > div:last-child {
    grid-column: 1 / -1;
  }

  .rotation-scorecard-mobile-metrics span {
    color: var(--el-text-color-secondary);
    font-size: 10px;
  }

  .rotation-scorecard-mobile-metrics strong {
    overflow: hidden;
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .rotation-scorecard-mobile-row p {
    margin: 7px 0 0;
    color: var(--el-color-warning-dark-2);
    font-size: 10px;
    line-height: 1.5;
  }

  .rotation-evaluation-header,
  .rotation-evaluation-footer {
    flex-direction: column;
    gap: 7px;
  }

  .rotation-evaluation-selection {
    width: 100%;
    justify-content: space-between;
    gap: 8px;
  }

  .rotation-evaluation-selection span:first-child {
    align-items: flex-start;
  }

  .rotation-evaluation-selection span:last-child {
    align-items: flex-end;
  }

  .rotation-evaluation-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .rotation-evaluation-footer strong {
    text-align: left;
  }

  .rotation-point-in-time-variant {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 9px 12px;
  }

  .rotation-point-in-time-variant-title {
    grid-column: 1 / -1;
  }

  .universe-table-view {
    display: none;
  }

  .universe-mobile-list {
    display: block;
    max-height: 520px;
    overflow-y: auto;
    border-top: 1px solid #ebeef5;
    border-bottom: 1px solid #ebeef5;
  }

  .universe-mobile-row {
    min-width: 0;
    padding: 12px 0;
    border-bottom: 1px solid #ebeef5;
  }

  .universe-mobile-row:last-child {
    border-bottom: 0;
  }

  .universe-mobile-heading {
    display: flex;
    min-width: 0;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
  }

  .universe-mobile-heading > div:first-child {
    display: flex;
    min-width: 0;
    flex-direction: column;
  }

  .universe-mobile-heading small {
    overflow: hidden;
    margin-top: 2px;
    color: #909399;
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .universe-mobile-heading .universe-state-tags {
    flex-shrink: 0;
    align-items: flex-end;
  }

  .universe-mobile-memberships {
    display: flex;
    gap: 4px;
    margin-top: 7px;
    flex-wrap: wrap;
  }

  .universe-mobile-metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
    margin-top: 10px;
  }

  .universe-mobile-metrics div {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 2px;
  }

  .universe-mobile-metrics span {
    color: #909399;
    font-size: 10px;
  }

  .universe-mobile-metrics strong {
    overflow: hidden;
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .universe-mobile-reason {
    margin-top: 9px;
    color: #606266;
    font-size: 11px;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  .universe-footer {
    flex-direction: column;
    gap: 2px;
  }

  .promotion-header {
    flex-direction: column;
    gap: 7px;
  }

  .promotion-meta {
    align-items: flex-start;
    flex-direction: row;
    gap: 5px 10px;
    flex-wrap: wrap;
  }

  .promotion-table-view {
    display: none;
  }

  .promotion-mobile-list {
    display: block;
    border-top: 1px solid #ebeef5;
    border-bottom: 1px solid #ebeef5;
  }

  .promotion-mobile-row {
    min-width: 0;
    padding: 12px 0;
    border-bottom: 1px solid #ebeef5;
  }

  .promotion-mobile-row:last-child {
    border-bottom: 0;
  }

  .promotion-mobile-heading {
    display: flex;
    min-width: 0;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
  }

  .promotion-mobile-heading > div:first-child {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 2px;
  }

  .promotion-mobile-heading small {
    color: #909399;
    font-size: 10px;
  }

  .promotion-mobile-tags {
    flex-shrink: 0;
    justify-content: flex-end;
  }

  .promotion-mobile-metrics {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 9px 12px;
    margin-top: 11px;
  }

  .promotion-mobile-metrics > div {
    display: flex;
    min-width: 0;
    align-items: flex-start;
    flex-direction: column;
    gap: 2px;
  }

  .promotion-mobile-metrics span,
  .promotion-mobile-blockers span {
    color: #909399;
    font-size: 10px;
  }

  .promotion-mobile-metrics strong,
  .promotion-mobile-blockers strong {
    max-width: 100%;
    font-size: 11px;
    overflow-wrap: anywhere;
  }

  .promotion-mobile-blockers {
    display: flex;
    min-width: 0;
    margin-top: 10px;
    flex-direction: column;
    gap: 3px;
  }
}
</style>
