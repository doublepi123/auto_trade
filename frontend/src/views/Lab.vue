<template>
  <div class="lab-page" data-testid="lab-page">
    <h2>LLM 优化工作台</h2>
    <el-tabs v-model="activeTab" data-testid="lab-tabs">
      <el-tab-pane label="实验与版本" name="experiments">
        <div data-testid="tab-experiments">
          <el-card header="Prompt 版本">
            <el-table :data="versions" data-testid="versions-table">
              <el-table-column prop="name" label="名称" />
              <el-table-column prop="version" label="版本" />
              <el-table-column prop="description" label="说明" />
              <el-table-column label="激活">
                <template #default="{ row }">
                  <el-tag v-if="row.is_active" type="success">激活中</el-tag>
                  <el-button v-else size="small" @click="activate(row)" data-testid="activate-btn">设为激活</el-button>
                </template>
              </el-table-column>
              <el-table-column prop="created_at" label="创建时间" />
            </el-table>
          </el-card>

          <el-card header="新建版本" style="margin-top: 12px">
            <el-form label-width="90px">
              <el-form-item label="名称"><el-input v-model="newVersion.name" data-testid="v-name" /></el-form-item>
              <el-form-item label="版本号"><el-input v-model="newVersion.version" data-testid="v-version" /></el-form-item>
              <el-form-item label="说明"><el-input v-model="newVersion.description" /></el-form-item>
              <el-form-item label="模板">
                <el-input v-model="newVersion.template" type="textarea" :rows="6" data-testid="v-template" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="creating" @click="submitVersion" data-testid="create-version-btn">创建</el-button>
              </el-form-item>
            </el-form>
          </el-card>

          <el-card header="实验摘要" style="margin-top: 12px">
            <el-select v-model="selectedSummaryExp" placeholder="选择实验" @change="loadSummary" data-testid="summary-exp-select">
              <el-option v-for="n in experimentNames" :key="n" :label="n" :value="n" />
            </el-select>
            <el-table :data="summary" style="margin-top: 8px">
              <el-table-column prop="variant_name" label="变体" />
              <el-table-column prop="total_count" label="样本" />
              <el-table-column prop="profitable_count" label="盈利数" />
              <el-table-column label="胜率"><template #default="{ row }">{{ pct(row.win_rate) }}</template></el-table-column>
              <el-table-column prop="avg_pnl" label="平均PnL" />
            </el-table>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="性能看板" name="performance">
        <div data-testid="tab-performance">
          <el-select v-model="perfExp" placeholder="选择实验" @change="loadPerformance" data-testid="perf-exp-select">
            <el-option v-for="n in experimentNames" :key="n" :label="n" :value="n" />
          </el-select>
          <el-empty v-if="!perfExp" description="请选择实验" />
          <template v-else>
            <el-row :gutter="12" style="margin-top: 12px" data-testid="perf-stats">
              <el-col :span="6"><el-statistic title="总交易" :value="Number(stats?.total_trades ?? 0)" /></el-col>
              <el-col :span="6"><el-statistic title="胜率" :value="Number((stats?.win_rate ?? 0) * 100)" suffix="%" /></el-col>
              <el-col :span="6"><el-statistic title="总PnL" :value="Number(stats?.total_pnl ?? 0)" /></el-col>
              <el-col :span="6"><el-statistic title="平均PnL" :value="Number(stats?.avg_pnl ?? 0)" /></el-col>
            </el-row>
            <el-table :data="variants" style="margin-top: 12px" data-testid="perf-variants">
              <el-table-column prop="variant" label="变体" />
              <el-table-column prop="total_trades" label="交易数" />
              <el-table-column label="胜率"><template #default="{ row }">{{ pct(row.win_rate) }}</template></el-table-column>
              <el-table-column prop="total_pnl" label="总PnL" />
              <el-table-column prop="avg_pnl" label="平均PnL" />
            </el-table>
            <el-card header="优化建议" style="margin-top: 12px" data-testid="perf-recommendations">
              <ul><li v-for="(r, i) in recommendations" :key="i">{{ r }}</li></ul>
            </el-card>
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane label="指标面板" name="indicators">
        <div data-testid="tab-indicators">
          <el-input v-model="indicatorSymbol" placeholder="标的（留空取当前策略）" style="width: 240px" data-testid="indicator-symbol" />
          <el-button type="primary" :loading="indicatorsLoading" @click="loadIndicators" data-testid="load-indicators-btn">查询</el-button>
          <span style="margin-left: 8px; color: #909399">实时快照，非历史复盘</span>

          <el-empty v-if="indicators && !indicators.available" description="行情不可用（broker 凭证缺失或限流）" data-testid="indicators-unavailable" />
          <el-row v-else-if="indicators && indicators.available" :gutter="12" style="margin-top: 12px" data-testid="indicators-grid">
            <el-col :span="8"><el-card header="RSI(14)">{{ indicators.rsi?.toFixed(2) }}</el-card></el-col>
            <el-col :span="8"><el-card header="MACD">macd {{ indicators.macd?.macd?.toFixed(3) }} / signal {{ indicators.macd?.signal?.toFixed(3) }} / hist {{ indicators.macd?.histogram?.toFixed(3) }}</el-card></el-col>
            <el-col :span="8"><el-card header="成交量">量比 {{ indicators.volume_analysis?.volume_ratio?.toFixed(2) }}（{{ indicators.volume_analysis?.trend }}）</el-card></el-col>
            <el-col :span="8" style="margin-top: 12px"><el-card header="市场情绪">{{ indicators.sentiment?.sentiment }}（{{ indicators.sentiment?.score?.toFixed(2) }}）<br>{{ indicators.sentiment?.description }}</el-card></el-col>
            <el-col :span="8" style="margin-top: 12px"><el-card header="多时间框架">{{ indicators.multi_timeframe?.description }}<br>对齐：{{ indicators.multi_timeframe?.aligned ? '是' : '否' }}</el-card></el-col>
            <el-col :span="8" style="margin-top: 12px"><el-card header="ATR / 布林带">ATR {{ indicators.atr?.toFixed(3) }}<br>上 {{ indicators.bb_upper?.toFixed(2) }} / 中 {{ indicators.bb_middle?.toFixed(2) }} / 下 {{ indicators.bb_lower?.toFixed(2) }}</el-card></el-col>
          </el-row>
        </div>
      </el-tab-pane>

      <el-tab-pane label="运行状态" name="runtime">
        <div data-testid="tab-runtime" v-loading="runtimeLoading">
          <div class="runtime-toolbar">
            <span class="muted">只读监控：LLM 调度状态、预算、symbol 状态与最近交互</span>
            <el-button type="primary" size="small" :loading="runtimeLoading" @click="loadRuntimeStatus">刷新</el-button>
          </div>

          <el-row v-if="runtimeStatus" :gutter="12" class="runtime-grid" data-testid="llm-runtime-overview">
            <el-col :xs="24" :sm="8">
              <el-card header="总览">
                <p><el-tag :type="runtimeStatus.enabled ? 'success' : 'info'">{{ runtimeStatus.enabled ? '已启用' : '未启用' }}</el-tag></p>
                <p>间隔：{{ runtimeStatus.interval_minutes }} 分钟</p>
                <p>最近分析：{{ formatDateTime(runtimeStatus.last_analysis_at) }}</p>
                <p>下次分析：{{ formatDateTime(runtimeStatus.next_analysis_at) }}</p>
              </el-card>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-card header="当前建议">
                <template v-if="runtimeStatus.current_suggestion">
                  <p>买入下沿 {{ runtimeStatus.current_suggestion.buy_low.toFixed(2) }}</p>
                  <p>卖出上沿 {{ runtimeStatus.current_suggestion.sell_high.toFixed(2) }}</p>
                  <p>置信度 {{ pct(runtimeStatus.current_suggestion.confidence_score) }}</p>
                  <p v-if="runtimeStatus.current_suggestion.analysis" class="runtime-analysis">{{ runtimeStatus.current_suggestion.analysis }}</p>
                </template>
                <el-empty v-else description="暂无建议" />
              </el-card>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-card header="应用值">
                <template v-if="runtimeStatus.applied_values">
                  <p>buy_low {{ runtimeStatus.applied_values.buy_low.toFixed(2) }}</p>
                  <p>sell_high {{ runtimeStatus.applied_values.sell_high.toFixed(2) }}</p>
                </template>
                <p v-if="runtimeStatus.reject_reason" class="negative">拒绝：{{ runtimeStatus.reject_reason }}</p>
                <el-empty v-if="!runtimeStatus.applied_values && !runtimeStatus.reject_reason" description="暂无应用记录" />
              </el-card>
            </el-col>
          </el-row>

          <el-card v-if="runtimeStatus" header="预算" class="runtime-card" data-testid="llm-runtime-budget">
            <el-row :gutter="12">
              <el-col :xs="12" :sm="4"><el-statistic title="每轮 symbol" :value="runtimeStatus.budget.max_symbols_per_cycle" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="每小时上限" :value="runtimeStatus.budget.max_analyses_per_hour" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="跟踪标的" :value="runtimeStatus.budget.tracked_symbol_count" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="有效预算" :value="runtimeStatus.budget.effective_symbol_budget" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="已用" :value="runtimeStatus.budget.used_analyses_last_hour" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="剩余" :value="runtimeStatus.budget.remaining_analyses_this_hour" /></el-col>
            </el-row>
            <div class="budget-bar" data-testid="llm-budget-progress">
              <div class="budget-bar-label">每小时用量</div>
              <el-progress
                :percentage="budgetUsagePct"
                :status="budgetUsagePct >= 90 ? 'exception' : budgetUsagePct >= 70 ? 'warning' : 'success'"
                :stroke-width="14"
              />
              <small class="budget-bar-note">{{ runtimeStatus.budget.used_analyses_last_hour }} / {{ runtimeStatus.budget.max_analyses_per_hour }} 次</small>
            </div>
          </el-card>

          <el-card class="runtime-card" data-testid="llm-usage-summary">
            <template #header>
              <div class="runtime-card-header">
                <span>大模型用量</span>
                <div class="usage-actions">
                  <el-select v-model="usageDays" size="small" data-testid="llm-usage-days" @change="loadLLMUsageSummary">
                    <el-option :value="7" label="近 7 天" />
                    <el-option :value="30" label="近 30 天" />
                    <el-option :value="90" label="近 90 天" />
                  </el-select>
                  <el-button size="small" :loading="usageLoading" data-testid="llm-usage-refresh" @click="loadLLMUsageSummary">刷新</el-button>
                </div>
              </div>
            </template>
            <DataState
              :loading="usageLoading"
              :error="usageError"
              :empty="usageSummary?.total_interactions === 0"
              empty-text="所选时间范围内暂无大模型调用"
            >
              <template v-if="usageSummary">
                <div class="usage-metrics" data-testid="llm-usage-metrics">
                  <MetricStat label="调用次数" :value="usageSummary.total_interactions" />
                  <MetricStat label="成功调用" :value="usageSummary.successful_interactions" />
                  <MetricStat label="输入 Token" :value="formatTokenCount(usageSummary.total_prompt_tokens)" />
                  <MetricStat label="输出 Token" :value="formatTokenCount(usageSummary.total_completion_tokens)" />
                  <MetricStat label="Token 总量" :value="formatTokenCount(usageSummary.total_tokens)" />
                </div>
                <el-table :data="usageSummary.by_day" size="small" class="usage-table" data-testid="llm-usage-daily">
                  <el-table-column prop="date" label="日期" min-width="110" />
                  <el-table-column prop="interactions" label="调用" min-width="80" />
                  <el-table-column label="输入 Token" min-width="120">
                    <template #default="{ row }">{{ formatTokenCount(row.prompt_tokens) }}</template>
                  </el-table-column>
                  <el-table-column label="输出 Token" min-width="120">
                    <template #default="{ row }">{{ formatTokenCount(row.completion_tokens) }}</template>
                  </el-table-column>
                  <el-table-column label="Token 总量" min-width="120">
                    <template #default="{ row }">{{ formatTokenCount(row.total_tokens) }}</template>
                  </el-table-column>
                </el-table>
                <div v-if="usageSummary.by_type.length" class="usage-types" data-testid="llm-usage-types">
                  <el-tag v-for="item in usageSummary.by_type" :key="item.interaction_type" effect="plain">
                    {{ item.interaction_type }}: {{ item.interactions }} 次 / {{ formatTokenCount(item.total_tokens) }} Token
                  </el-tag>
                </div>
              </template>
            </DataState>
          </el-card>

          <el-card v-if="runtimeHealthHints.length" header="健康提示" class="runtime-card" data-testid="llm-runtime-health">
            <el-alert
              v-for="hint in runtimeHealthHints"
              :key="hint"
              :title="hint"
              type="warning"
              show-icon
              :closable="false"
              class="runtime-alert"
            />
          </el-card>

          <el-card v-if="runtimeStatus" header="Symbol 状态" class="runtime-card" data-testid="llm-runtime-symbols">
            <el-table :data="runtimeStatus.symbol_statuses" size="small">
              <el-table-column prop="symbol" label="标的" min-width="110">
                <template #default="{ row }">
                  <strong>{{ row.symbol }}</strong>
                  <el-tag v-if="row.is_primary" size="small" type="primary" style="margin-left: 4px">主</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="market" label="市场" width="80" />
              <el-table-column label="挂单" width="80">
                <template #default="{ row }">{{ row.has_pending_order ? '有' : '无' }}</template>
              </el-table-column>
              <el-table-column label="最近分析" min-width="170">
                <template #default="{ row }">{{ formatDateTime(row.last_analysis_at) }}</template>
              </el-table-column>
              <el-table-column label="下次分析" min-width="170">
                <template #default="{ row }">{{ formatDateTime(row.next_analysis_at) }}</template>
              </el-table-column>
              <el-table-column prop="last_status" label="状态" min-width="100" />
              <el-table-column prop="last_skip_reason" label="跳过原因" min-width="180">
                <template #default="{ row }">{{ row.last_skip_reason || '-' }}</template>
              </el-table-column>
            </el-table>
          </el-card>

          <el-card header="最近交互" class="runtime-card" data-testid="llm-runtime-interactions">
            <template #header>
              <div class="runtime-card-header">
                <span>最近交互</span>
                <div>
                  <el-button size="small" plain :disabled="runtimeStatus == null" data-testid="lab-export-symbols" @click="exportSymbolStatus">导出 Symbol 状态</el-button>
                  <el-button size="small" plain :disabled="runtimeInteractions.length === 0" data-testid="lab-export-interactions" @click="exportInteractions">导出交互 CSV</el-button>
                </div>
              </div>
            </template>
            <el-table :data="runtimeInteractions" size="small">
              <el-table-column prop="id" label="ID" width="70" />
              <el-table-column prop="interaction_type" label="类型" width="100" />
              <el-table-column prop="symbol" label="标的" width="110" />
              <el-table-column label="结果" width="100">
                <template #default="{ row }"><el-tag :type="row.success ? 'success' : 'danger'">{{ row.success ? '成功' : '失败' }}</el-tag></template>
              </el-table-column>
              <el-table-column label="应用" width="90">
                <template #default="{ row }"><el-tag :type="row.applied ? 'primary' : 'info'">{{ row.applied ? '已应用' : '未应用' }}</el-tag></template>
              </el-table-column>
              <el-table-column prop="order_action" label="动作" width="100" />
              <el-table-column prop="error" label="错误" min-width="160">
                <template #default="{ row }">{{ row.error || '-' }}</template>
              </el-table-column>
              <el-table-column label="时间" min-width="170">
                <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="策略 v2 影子" name="strategy-shadow">
        <div data-testid="tab-strategy-shadow" v-loading="shadowLoading">
          <section
            class="shadow-section opening-momentum-section"
            data-testid="opening-momentum-shadow"
          >
            <div class="shadow-section-header">
              <div>
                <h3>开盘横截面动量</h3>
                <small v-if="openingMomentumStatus">
                  {{ openingMomentumStatus.config.algorithm_version }} ·
                  {{ shortVersion(openingMomentumStatus.config.config_version) }}
                </small>
              </div>
              <div class="shadow-tags">
                <el-tag type="warning">影子观察</el-tag>
                <el-tag type="danger" effect="plain">永不下单</el-tag>
                <el-tag :type="openingMomentumStateType" effect="plain">
                  {{ openingMomentumStateLabel }}
                </el-tag>
              </div>
            </div>

            <el-alert
              v-if="openingMomentumLoadError"
              :title="openingMomentumLoadError"
              type="warning"
              :closable="false"
              show-icon
              data-testid="opening-momentum-error"
            />

            <template v-if="openingMomentumStatus">
              <div class="shadow-facts" data-testid="opening-momentum-config">
                <div>
                  <span>信号窗口</span>
                  <strong>{{ openingMomentumStatus.config.signal_minutes }} 分钟</strong>
                </div>
                <div>
                  <span>执行延迟</span>
                  <strong>{{ openingMomentumStatus.config.execution_delay_minutes }} 分钟</strong>
                </div>
                <div>
                  <span>虚拟持仓</span>
                  <strong>{{ openingMomentumStatus.config.holding_minutes }} 分钟</strong>
                </div>
                <div>
                  <span>最小池规模</span>
                  <strong>{{ openingMomentumStatus.config.minimum_universe_size }}</strong>
                </div>
                <div>
                  <span>市场门槛</span>
                  <strong>{{ formatBps(openingMomentumStatus.config.minimum_market_return_bps) }}</strong>
                </div>
                <div>
                  <span>相对强度门槛</span>
                  <strong>{{ formatBps(openingMomentumStatus.config.minimum_excess_return_bps) }}</strong>
                </div>
                <div>
                  <span>往返成本</span>
                  <strong>{{ formatBps(openingMomentumStatus.config.round_trip_cost_bps) }}</strong>
                </div>
              </div>

              <div
                v-if="openingMomentumStatus.latest"
                class="opening-momentum-latest"
                data-testid="opening-momentum-latest"
              >
                <div class="shadow-section-header">
                  <div>
                    <h3>{{ openingMomentumStatus.latest.session_date }}</h3>
                    <small>{{ openingMomentumStatus.latest.reason }}</small>
                  </div>
                  <el-tag
                    :type="openingMomentumStatus.latest.status === 'CLOSED' ? 'success' : openingMomentumStatus.latest.status === 'OPEN' ? 'warning' : 'info'"
                    effect="plain"
                  >
                    {{ openingMomentumStatus.latest.status }}
                  </el-tag>
                </div>
                <div class="shadow-facts">
                  <div>
                    <span>候选</span>
                    <strong>{{ openingMomentumStatus.latest.candidate_symbol || '-' }}</strong>
                  </div>
                  <div>
                    <span>有效池</span>
                    <strong>{{ openingMomentumStatus.latest.universe_size }}</strong>
                  </div>
                  <div>
                    <span>市场中位</span>
                    <strong>{{ formatNullableBps(openingMomentumStatus.latest.market_return_bps) }}</strong>
                  </div>
                  <div>
                    <span>候选涨幅</span>
                    <strong>{{ formatNullableBps(openingMomentumStatus.latest.candidate_return_bps) }}</strong>
                  </div>
                  <div>
                    <span>相对强度</span>
                    <strong>{{ formatNullableBps(openingMomentumStatus.latest.excess_return_bps) }}</strong>
                  </div>
                  <div>
                    <span>成本后收益</span>
                    <strong :class="{ negative: (openingMomentumStatus.latest.net_return_bps ?? 0) < 0 }">
                      {{ formatNullableBps(openingMomentumStatus.latest.net_return_bps) }}
                    </strong>
                  </div>
                </div>
              </div>
              <el-empty v-else description="等待下一次美股开盘观测" />

              <div
                class="shadow-metrics-grid opening-momentum-metrics"
                data-testid="opening-momentum-metrics"
              >
                <el-statistic title="观测日" :value="openingMomentumStatus.metrics.observed_sessions" />
                <el-statistic title="闭环交易" :value="openingMomentumStatus.metrics.closed_trades" />
                <el-statistic title="胜率" :value="openingMomentumStatus.metrics.win_rate * 100" suffix="%" :precision="1" />
                <el-statistic title="平均净收益" :value="openingMomentumStatus.metrics.mean_net_return_bps" suffix=" bps" :precision="1" />
                <el-statistic title="累计净收益" :value="openingMomentumStatus.metrics.cumulative_net_return_bps" suffix=" bps" :precision="1" />
                <el-statistic title="最大回撤" :value="openingMomentumStatus.metrics.max_drawdown_bps" suffix=" bps" :precision="1" />
                <el-statistic title="跳过日" :value="openingMomentumStatus.metrics.skipped_sessions" />
                <div class="opening-momentum-stat">
                  <span>利润因子</span>
                  <strong>{{ formatNullable(openingMomentumStatus.metrics.profit_factor) }}</strong>
                </div>
              </div>

              <div
                v-if="openingMomentumStatus.variants.length > 1"
                class="opening-momentum-variants"
                data-testid="opening-momentum-variants"
              >
                <h4 class="shadow-subsection-title">同场选池对照</h4>
                <el-table
                  :data="openingMomentumStatus.variants"
                  size="small"
                >
                  <el-table-column label="策略" min-width="140">
                    <template #default="{ row }">
                      <el-tag
                        :type="row.variant === 'INCUMBENT' ? 'primary' : 'warning'"
                        effect="plain"
                      >
                        {{ openingMomentumVariantLabel(row.variant) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="当日候选" min-width="110">
                    <template #default="{ row }">
                      {{ row.latest?.candidate_symbol || '-' }}
                    </template>
                  </el-table-column>
                  <el-table-column
                    prop="comparison_sessions"
                    label="同场样本"
                    min-width="90"
                  />
                  <el-table-column label="信号" min-width="70">
                    <template #default="{ row }">
                      {{ row.metrics.signals }}
                    </template>
                  </el-table-column>
                  <el-table-column label="闭环" min-width="70">
                    <template #default="{ row }">
                      {{ row.metrics.closed_trades }}
                    </template>
                  </el-table-column>
                  <el-table-column label="胜率" min-width="85">
                    <template #default="{ row }">
                      {{ (row.metrics.win_rate * 100).toFixed(1) }}%
                    </template>
                  </el-table-column>
                  <el-table-column label="平均净收益" min-width="110">
                    <template #default="{ row }">
                      {{ formatBps(row.metrics.mean_net_return_bps) }}
                    </template>
                  </el-table-column>
                  <el-table-column label="累计净收益" min-width="110">
                    <template #default="{ row }">
                      {{ formatBps(row.metrics.cumulative_net_return_bps) }}
                    </template>
                  </el-table-column>
                  <el-table-column label="利润因子" min-width="90">
                    <template #default="{ row }">
                      {{ formatNullable(row.metrics.profit_factor) }}
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </template>
          </section>

          <div class="shadow-toolbar">
            <div class="shadow-tags" data-testid="shadow-safety-tags">
              <el-tag type="warning">影子观察</el-tag>
              <el-tag type="danger" effect="plain">永不下单</el-tag>
              <el-tag :type="shadowConfig?.enabled ? 'success' : 'info'">
                {{ shadowConfig?.enabled ? '采集中' : '已停用' }}
              </el-tag>
              <el-tag effect="plain">1m 触发</el-tag>
              <el-tag effect="plain">5m 确认</el-tag>
            </div>
            <div class="shadow-toolbar-actions">
              <el-select
                v-model="selectedShadowSymbol"
                size="small"
                placeholder="选择标的"
                data-testid="shadow-symbol-select"
                @change="loadStrategyShadow"
              >
                <el-option
                  v-for="item in shadowConfigs"
                  :key="item.symbol"
                  :label="`${item.symbol} · ${item.enabled ? '采集中' : '已停用'}`"
                  :value="item.symbol"
                />
              </el-select>
              <el-select
                v-model="selectedShadowVersion"
                size="small"
                placeholder="证据版本"
                data-testid="shadow-version-select"
                @change="loadShadowEvidence"
              >
                <el-option
                  v-for="item in shadowVersions"
                  :key="item.config_version"
                  :label="`${shortVersion(item.config_version)}${item.current ? ' · 当前' : ''}`"
                  :value="item.config_version"
                />
              </el-select>
              <el-button
                type="primary"
                size="small"
                :loading="shadowLoading"
                data-testid="shadow-refresh"
                @click="loadStrategyShadow()"
              >刷新</el-button>
            </div>
          </div>

          <el-alert
            v-if="shadowLoadError"
            :title="shadowLoadError"
            type="error"
            :closable="false"
            show-icon
            data-testid="shadow-load-error"
          />

          <el-alert
            v-if="shadowStatus?.last_poll_error"
            :title="`最近轮询失败：${shadowStatus.last_poll_error}`"
            type="warning"
            :closable="false"
            show-icon
            data-testid="shadow-poll-error"
          />

          <el-alert
            v-if="shadowAdxLoading"
            title="正在计算 ADX 同样本对照"
            type="info"
            :closable="false"
            show-icon
            data-testid="shadow-adx-loading"
          />

          <el-alert
            v-if="shadowAdxChallengerError"
            :title="shadowAdxChallengerError"
            type="warning"
            :closable="false"
            show-icon
            data-testid="shadow-adx-error"
          />

          <template v-if="shadowConfig && shadowStatus">
            <el-alert
              v-if="shadowStatus.version_transition_pending"
              :title="shadowVersionTransitionTitle"
              type="warning"
              :closable="false"
              show-icon
              data-testid="shadow-version-transition"
            />

            <section class="shadow-section" data-testid="shadow-config-section">
              <div class="shadow-section-header">
                <div>
                  <h3>采集配置</h3>
                  <small>
                    {{ shadowConfig.symbol || '-' }} · 当前 {{ shortVersion(shadowStatus.config.config_version) }} ·
                    {{ shadowStatus.phase }} · 轮询 {{ formatDateTime(shadowStatus.last_polled_at) }}
                  </small>
                </div>
                <el-button
                  type="primary"
                  :loading="shadowSaving"
                  data-testid="shadow-save-config"
                  @click="saveShadowConfig"
                >保存配置</el-button>
              </div>

              <el-form label-position="top" class="shadow-form">
                <el-form-item label="采集">
                  <el-switch
                    v-model="shadowForm.enabled"
                    active-text="启用"
                    inactive-text="停用"
                    data-testid="shadow-enabled"
                  />
                </el-form-item>
                <el-form-item label="1m z-score 窗口">
                  <el-input-number v-model="shadowForm.zscore_window_1m_bars" :min="10" :max="240" :step="5" data-testid="shadow-window-1m" />
                </el-form-item>
                <el-form-item label="5m z-score 窗口">
                  <el-input-number v-model="shadowForm.zscore_window_5m_bars" :min="5" :max="shadowMax5mWindow" :step="5" data-testid="shadow-window-5m" />
                </el-form-item>
                <el-form-item label="跌破阈值">
                  <el-input-number v-model="shadowForm.breach_zscore" :min="-5" :max="-0.5" :step="0.1" :precision="2" data-testid="shadow-breach-z" />
                </el-form-item>
                <el-form-item label="收复阈值">
                  <el-input-number v-model="shadowForm.reclaim_zscore" :min="-3" :max="0" :step="0.1" :precision="2" data-testid="shadow-reclaim-z" />
                </el-form-item>
                <el-form-item label="5m z-score 上限">
                  <el-input-number v-model="shadowForm.five_minute_zscore_max" :min="-5" :max="0" :step="0.1" :precision="2" data-testid="shadow-five-minute-z" />
                </el-form-item>
                <el-form-item label="ADX 周期">
                  <el-input-number v-model="shadowForm.adx_period" :min="5" :max="shadowMaxAdxPeriod" data-testid="shadow-adx-period" />
                </el-form-item>
                <el-form-item label="ADX 上限">
                  <el-input-number v-model="shadowForm.max_adx" :min="1" :max="40" :step="1" :precision="1" data-testid="shadow-max-adx" />
                </el-form-item>
                <el-form-item label="波动率窗口">
                  <el-input-number v-model="shadowForm.realized_vol_window_bars" :min="10" :max="240" :step="5" data-testid="shadow-vol-window" />
                </el-form-item>
                <el-form-item label="波动率下限">
                  <el-input-number v-model="shadowForm.min_realized_vol" :min="0" :max="3" :step="0.001" :precision="4" data-testid="shadow-min-vol" />
                </el-form-item>
                <el-form-item label="波动率上限">
                  <el-input-number v-model="shadowForm.max_realized_vol" :min="0" :max="3" :step="0.001" :precision="4" data-testid="shadow-max-vol" />
                </el-form-item>
                <el-form-item label="止损">
                  <el-input-number v-model="shadowForm.stop_loss_pct" :min="0.05" :max="0.75" :step="0.05" :precision="2" data-testid="shadow-stop-loss" />
                </el-form-item>
                <el-form-item label="止盈">
                  <el-input-number v-model="shadowForm.profit_target_pct" :min="0.05" :max="5" :step="0.05" :precision="2" data-testid="shadow-profit-target" />
                </el-form-item>
              </el-form>
            </section>

            <section class="shadow-section" data-testid="shadow-hard-safety">
              <div class="shadow-section-header"><h3>硬安全线</h3></div>
              <div class="shadow-facts">
                <div><span>最长持仓</span><strong>{{ shadowConfig.max_holding_minutes }} 分钟</strong></div>
                <div><span>停止开仓</span><strong>收盘前 {{ shadowConfig.entry_cutoff_minutes_before_close }} 分钟</strong></div>
                <div><span>强制平仓</span><strong>收盘前 {{ shadowConfig.flatten_minutes_before_close }} 分钟</strong></div>
                <div><span>单日上限</span><strong>{{ shadowConfig.max_entries_per_day }} 次</strong></div>
                <div><span>退出冷却</span><strong>{{ shadowConfig.entry_cooldown_minutes }} 分钟</strong></div>
                <div><span>武装有效期</span><strong>{{ shadowConfig.arm_ttl_bars }} bars</strong></div>
                <div><span>虚拟滑点</span><strong>{{ shadowConfig.slippage_bps.toFixed(1) }} bps</strong></div>
                <div><span>美股单边费率</span><strong>{{ formatPercent(shadowConfig.estimated_fee_rate_us) }}</strong></div>
                <div><span>港股单边费率</span><strong>{{ formatPercent(shadowConfig.estimated_fee_rate_hk) }}</strong></div>
                <div><span>加仓</span><strong>{{ shadowConfig.allow_position_addons ? '允许' : '禁止' }}</strong></div>
                <div><span>做空</span><strong>{{ shadowConfig.short_entries_enabled ? '允许' : '禁止' }}</strong></div>
                <div><span>订单提交</span><strong>{{ shadowConfig.order_submission_allowed ? '允许' : '禁止' }}</strong></div>
                <div><span>算法版本</span><strong>{{ shadowConfig.algorithm_version }}</strong></div>
              </div>
            </section>

            <section class="shadow-section" data-testid="shadow-latest-signal">
              <div class="shadow-section-header">
                <h3>当前信号</h3>
                <div class="shadow-tags">
                  <el-tag effect="plain">证据 {{ shortVersion(shadowStatus.evidence_config_version) }}</el-tag>
                  <el-tag v-if="shadowStatus.latest" :type="shadowFreshnessType" effect="plain">{{ shadowFreshnessLabel }}</el-tag>
                </div>
              </div>
              <el-empty v-if="!shadowStatus.latest" description="暂无影子信号" />
              <template v-else>
                <div class="shadow-facts shadow-signal-grid">
                  <div><span>价格</span><strong>{{ formatNullable(shadowStatus.latest.price) }}</strong></div>
                  <div><span>1m VWAP / z</span><strong>{{ formatNullable(shadowStatus.latest.vwap_1m) }} / {{ formatNullable(shadowStatus.latest.zscore_1m) }}</strong></div>
                  <div><span>5m VWAP / z</span><strong>{{ formatNullable(shadowStatus.latest.vwap_5m) }} / {{ formatNullable(shadowStatus.latest.zscore_5m) }}</strong></div>
                  <div><span>ADX</span><strong>{{ formatNullable(shadowStatus.latest.adx) }}</strong></div>
                  <div><span>实现波动率</span><strong>{{ formatNullable(shadowStatus.latest.realized_vol, 4) }}</strong></div>
                  <div><span>Regime</span><strong>{{ shadowStatus.latest.regime_eligible ? '通过' : '拦截' }}</strong></div>
                  <div><span>跌破状态</span><strong>{{ shadowStatus.latest.breach_armed ? '已武装' : '未触发' }}</strong></div>
                  <div><span>虚拟持仓</span><strong>{{ shadowPositionLabel(shadowStatus.latest.virtual_position) }}</strong></div>
                  <div><span>最近动作</span><strong>{{ shadowStatus.latest.last_action || '-' }}</strong></div>
                </div>
                <p class="shadow-reason" data-testid="shadow-latest-reason">{{ shadowStatus.latest.last_reason || '-' }}</p>
              </template>
            </section>

            <section class="shadow-section" data-testid="shadow-metrics">
              <div class="shadow-section-header">
                <h3>影子表现</h3>
                <el-tag effect="plain">证据 {{ shortVersion(shadowStatus.evidence_config_version) }}</el-tag>
              </div>
              <div class="shadow-metrics-grid">
                <el-statistic title="闭环交易" :value="shadowStatus.metrics.closed_trades" />
                <el-statistic title="净收益" :value="shadowStatus.metrics.net_pnl" :precision="2" />
                <el-statistic title="胜率" :value="shadowStatus.metrics.win_rate * 100" suffix="%" :precision="1" />
                <el-statistic title="最大回撤" :value="shadowStatus.metrics.max_drawdown" :precision="2" />
                <el-statistic title="平均持仓" :value="shadowStatus.metrics.avg_holding_minutes" suffix="m" :precision="1" />
                <el-statistic title="有效 bar" :value="shadowStatus.metrics.eligible_bars" />
              </div>
              <el-alert
                v-if="!shadowStatus.metrics.comparison_available"
                title="尚未建立版本一致的实盘对照基线"
                type="info"
                :closable="false"
                show-icon
                data-testid="shadow-comparison-unavailable"
              />
              <div class="shadow-excursions">
                <span>平均 MAE {{ formatPercent(shadowStatus.metrics.avg_mae_pct) }}</span>
                <span>平均 MFE {{ formatPercent(shadowStatus.metrics.avg_mfe_pct) }}</span>
                <span>费用 {{ shadowStatus.metrics.fees.toFixed(2) }}</span>
                <span>跌破 / 收复 {{ shadowStatus.metrics.breaches }} / {{ shadowStatus.metrics.reclaims }}</span>
              </div>
            </section>

            <section v-if="shadowEvaluation" class="shadow-section" data-testid="shadow-evaluation">
              <div class="shadow-section-header">
                <div>
                  <h3>证据成熟度</h3>
                  <small>{{ shortVersion(shadowEvaluation.config_version) }} · 仅供复核，不会自动晋级或下单</small>
                </div>
                <el-tag :type="shadowEvaluation.status === 'READY_FOR_REVIEW' ? 'success' : 'warning'">
                  {{ shadowEvaluation.status === 'READY_FOR_REVIEW' ? '可复核' : '采集中' }}
                </el-tag>
              </div>
              <div class="shadow-progress-grid">
                <div>
                  <span>交易日 {{ shadowEvaluation.observed_trading_days }} / {{ shadowEvaluation.minimum_trading_days }}</span>
                  <el-progress :percentage="shadowDayProgress" :stroke-width="8" />
                </div>
                <div>
                  <span>闭环交易 {{ shadowEvaluation.eligible_closed_trades }} / {{ shadowEvaluation.minimum_closed_trades }}</span>
                  <el-progress :percentage="shadowTradeProgress" :stroke-width="8" />
                </div>
              </div>
              <div class="shadow-evidence-summary" data-testid="shadow-evidence-excluded">
                <span>排除交易日 {{ shadowEvaluation.excluded_trading_days }}</span>
                <span>排除闭环交易 {{ shadowEvaluation.excluded_closed_trades }}</span>
              </div>
              <el-alert
                v-if="shadowEvaluation.readiness_blockers.length"
                title="当前阻塞"
                :description="shadowBlockerSummary"
                type="warning"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-evidence-blockers"
              />
              <el-alert
                v-if="shadowEvaluation.data_quality_warnings.length"
                title="数据质量提示"
                :description="shadowEvaluation.data_quality_warnings.join('；')"
                type="info"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-evidence-warnings"
              />
              <el-table
                :data="shadowEvaluation.daily"
                size="small"
                empty-text="暂无按日证据"
                data-testid="shadow-evidence-daily"
              >
                <el-table-column type="expand" width="44">
                  <template #default="{ row }">
                    <el-table :data="row.hourly_eligibility" size="small" empty-text="暂无分时证据">
                      <el-table-column label="市场时段" width="130">
                        <template #default="scope">{{ formatShadowSessionHour(scope.row.session_hour) }}</template>
                      </el-table-column>
                      <el-table-column prop="bars" label="bar" width="70" />
                      <el-table-column prop="ready_bars" label="指标就绪" width="90" />
                      <el-table-column prop="eligible_bars" label="Gate 通过" width="90" />
                      <el-table-column label="通过率" width="90">
                        <template #default="scope">{{ formatEligibilityRate(scope.row.eligible_bars, scope.row.bars) }}</template>
                      </el-table-column>
                      <el-table-column label="主要 Gate" min-width="180">
                        <template #default="scope">{{ formatGateCounts(scope.row.gate_counts) }}</template>
                      </el-table-column>
                    </el-table>
                  </template>
                </el-table-column>
                <el-table-column prop="session_date" label="交易日" width="120" />
                <el-table-column prop="bars" label="bar" width="80" />
                <el-table-column label="首次就绪（市场）" width="135">
                  <template #default="{ row }">{{ formatMarketClock(row.first_ready_at) }}</template>
                </el-table-column>
                <el-table-column label="就绪 / 预热损失" width="125">
                  <template #default="{ row }">{{ row.ready_bars }} / {{ row.warmup_lost_bars }}</template>
                </el-table-column>
                <el-table-column label="覆盖率" width="100">
                  <template #default="{ row }">{{ formatPercent(row.coverage_ratio) }}</template>
                </el-table-column>
                <el-table-column prop="missing_internal_bars" label="缺口" width="80" />
                <el-table-column prop="incomplete_feature_bars" label="特征缺失" width="90" />
                <el-table-column prop="trades" label="交易" width="80" />
                <el-table-column prop="net_pnl" label="净收益" min-width="100" />
              </el-table>
            </section>

            <section
              v-if="shadowWarmupDiagnostic"
              class="shadow-section"
              data-testid="shadow-warmup-diagnostic"
            >
              <div class="shadow-section-header">
                <div>
                  <h3>预热与分时可用性</h3>
                  <small>
                    {{ shadowWarmupDiagnostic.algorithm_version }} ·
                    {{ shortVersion(shadowAdxChallengers?.source_config_version || '') }}
                  </small>
                </div>
                <div class="shadow-tags">
                  <el-tag effect="plain">只读影子</el-tag>
                  <el-tag :type="shadowWarmupStatusMeta.type" data-testid="shadow-warmup-status">
                    {{ shadowWarmupStatusMeta.label }}
                  </el-tag>
                </div>
              </div>
              <div class="shadow-evidence-summary">
                <span>
                  因果配对 {{ shadowWarmupDiagnostic.evaluated_causal_pairs }} /
                  {{ shadowWarmupDiagnostic.minimum_causal_pairs }}
                </span>
                <span>观测配对 {{ shadowWarmupDiagnostic.observed_causal_pairs }}</span>
                <span>同样本 {{ shadowWarmupDiagnostic.same_sample ? '是' : '否' }}</span>
                <span>仅历史上下文 {{ shadowWarmupDiagnostic.causal_history_only ? '是' : '否' }}</span>
              </div>
              <el-alert
                title="仅预热 ADX / 波动率"
                description="VWAP 与 z-score 仍按交易日重置；结果不可晋级、不会写入状态或提交订单。"
                type="info"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-warmup-readonly"
              />
              <el-alert
                v-if="shadowWarmupDiagnostic.status === 'INSUFFICIENT_EVIDENCE'"
                title="因果配对交易日不足"
                :description="`当前可评估 ${shadowWarmupDiagnostic.evaluated_causal_pairs} 对，至少需要 ${shadowWarmupDiagnostic.minimum_causal_pairs} 对。`"
                type="warning"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-warmup-insufficient"
              />
              <el-alert
                v-if="shadowWarmupDiagnostic.status === 'BLOCKED'"
                title="因果预热诊断已阻塞"
                :description="shadowWarmupBlockerSummary || '诊断校验未通过，暂不可复核。'"
                type="error"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-warmup-blocked"
              />
              <el-table
                :data="shadowWarmupVariantRows"
                size="small"
                empty-text="暂无预热对照"
                data-testid="shadow-warmup-variants"
              >
                <el-table-column type="expand" width="44">
                  <template #default="{ row }">
                    <el-table :data="row.daily" size="small" empty-text="暂无配对交易日">
                      <el-table-column prop="session_date" label="目标日" width="110" />
                      <el-table-column label="种子日" width="110">
                        <template #default="scope">{{ scope.row.seed_session_date || '-' }}</template>
                      </el-table-column>
                      <el-table-column label="上下文截止（市场）" width="145">
                        <template #default="scope">{{ formatMarketDateTime(scope.row.trend_context_cutoff_at) }}</template>
                      </el-table-column>
                      <el-table-column label="隔夜跳空" width="100">
                        <template #default="scope">{{ formatNullablePercent(scope.row.overnight_gap_pct) }}</template>
                      </el-table-column>
                      <el-table-column label="首次就绪（市场）" width="135">
                        <template #default="scope">{{ formatMarketClock(scope.row.first_ready_at) }}</template>
                      </el-table-column>
                      <el-table-column label="就绪 / 预热损失" width="125">
                        <template #default="scope">{{ scope.row.ready_bars }} / {{ scope.row.warmup_lost_bars }}</template>
                      </el-table-column>
                      <el-table-column prop="eligible_bars" label="Gate 通过" width="95" />
                    </el-table>
                  </template>
                </el-table-column>
                <el-table-column label="方案" min-width="170">
                  <template #default="{ row }">
                    <el-tag :type="row.label === 'SESSION_LOCAL' ? 'info' : 'primary'" effect="plain">
                      {{ shadowWarmupVariantLabel(row.label) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="预热范围" width="125">
                  <template #default="{ row }">{{ row.warmup_scope === 'ADX_VOL_ONLY' ? 'ADX / 波动率' : '无' }}</template>
                </el-table-column>
                <el-table-column prop="bars" label="bar" width="80" />
                <el-table-column prop="readyBars" label="指标就绪" width="95" />
                <el-table-column prop="warmupLostBars" label="预热损失" width="95" />
                <el-table-column prop="eligibleBars" label="Gate 通过" width="95" />
                <el-table-column label="恢复就绪" width="95">
                  <template #default="{ row }">{{ formatSignedCount(row.recoveredReadyBars) }}</template>
                </el-table-column>
                <el-table-column label="Gate 增量" width="95">
                  <template #default="{ row }">{{ formatSignedCount(row.eligibleDelta) }}</template>
                </el-table-column>
              </el-table>

              <h4 class="shadow-subsection-title">市场分时对照</h4>
              <el-table
                :data="shadowWarmupHourlyRows"
                size="small"
                empty-text="暂无分时对照"
                data-testid="shadow-warmup-hourly"
              >
                <el-table-column prop="sessionLabel" label="市场时段" width="130" />
                <el-table-column label="bar 基线 / 预热" width="125">
                  <template #default="{ row }">{{ row.baselineBars }} / {{ row.prewarmBars }}</template>
                </el-table-column>
                <el-table-column label="就绪 基线 / 预热" width="135">
                  <template #default="{ row }">{{ row.baselineReady }} / {{ row.prewarmReady }}</template>
                </el-table-column>
                <el-table-column label="Gate 基线 / 预热" width="135">
                  <template #default="{ row }">{{ row.baselineEligible }} / {{ row.prewarmEligible }}</template>
                </el-table-column>
                <el-table-column label="恢复就绪" width="95">
                  <template #default="{ row }">{{ formatSignedCount(row.readyDelta) }}</template>
                </el-table-column>
                <el-table-column label="Gate 增量" width="95">
                  <template #default="{ row }">{{ formatSignedCount(row.eligibleDelta) }}</template>
                </el-table-column>
                <el-table-column label="预热方案主要 Gate" min-width="190">
                  <template #default="{ row }">{{ formatGateCounts(row.prewarmGateCounts) }}</template>
                </el-table-column>
              </el-table>
            </section>

            <section class="shadow-section" data-testid="shadow-forward-validation">
              <div class="shadow-section-header">
                <div>
                  <h3>因果预热前向验证</h3>
                  <small v-if="shadowForwardRegistration">
                    注册 #{{ shadowForwardRegistration.id }} ·
                    {{ shortVersion(shadowForwardRegistration.source_config_version) }} ·
                    {{ shadowForwardRegistration.candidate_algorithm_version }}
                  </small>
                  <small v-else>
                    固定候选 {{ shadowForwardCandidateVersion }}
                  </small>
                </div>
                <div class="shadow-tags">
                  <el-tag effect="plain">纯影子</el-tag>
                  <el-tag
                    v-if="shadowForwardValidation"
                    :type="shadowForwardStatusMeta.type"
                    data-testid="shadow-forward-status"
                  >
                    {{ shadowForwardStatusMeta.label }}
                  </el-tag>
                  <el-button
                    v-if="!shadowForwardRegistration"
                    type="primary"
                    size="small"
                    :disabled="!shadowForwardCanRegister"
                    :loading="shadowForwardRegistering"
                    data-testid="shadow-forward-register"
                    @click="openShadowForwardDialog"
                  >
                    <el-icon><Lock /></el-icon>
                    <span>冻结前向验证</span>
                  </el-button>
                </div>
              </div>

              <el-alert
                v-if="shadowForwardLoading"
                title="正在读取前向验证"
                type="info"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-forward-loading"
              />
              <el-alert
                v-if="shadowForwardError"
                :title="shadowForwardError"
                type="error"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-forward-error"
              />

              <template v-if="shadowForwardValidation">
                <div
                  v-if="shadowForwardRegistration"
                  class="shadow-facts shadow-forward-facts"
                  data-testid="shadow-forward-registration"
                >
                  <div>
                    <span>注册时间（市场）</span>
                    <strong>{{ formatForwardDateTime(shadowForwardRegistration.registered_at) }}</strong>
                  </div>
                  <div>
                    <span>最早纳入（市场）</span>
                    <strong>{{ formatForwardDateTime(shadowForwardRegistration.eligible_after) }}</strong>
                  </div>
                  <div>
                    <span>评估器摘要</span>
                    <strong>{{ shadowForwardRegistration.evaluator_digest.slice(0, 12) }}</strong>
                  </div>
                </div>

                <el-alert
                  v-if="shadowForwardRegistration
                    && shadowForwardRegistration.source_config_version !== selectedShadowVersion"
                  title="前向 cohort 属于另一证据版本"
                  :description="`此处始终展示 ${shortVersion(shadowForwardRegistration.source_config_version)} 的冻结前向证据，与当前选择的 ${shortVersion(selectedShadowVersion)} 不混合。`"
                  type="info"
                  :closable="false"
                  show-icon
                  class="shadow-evidence-alert"
                  data-testid="shadow-forward-version-context"
                />

                <div
                  v-if="shadowForwardRegistration"
                  class="shadow-evidence-summary shadow-forward-summary"
                  data-testid="shadow-forward-summary"
                >
                  <span>
                    初步复核 {{ shadowForwardValidation.included_pairs }} /
                    {{ shadowForwardRegistration.minimum_ready_pairs }}
                  </span>
                  <span>
                    成熟证据 {{ shadowForwardValidation.included_pairs }} /
                    {{ shadowForwardRegistration.minimum_mature_pairs }}
                  </span>
                  <span>排除目标 {{ shadowForwardValidation.excluded_targets }}</span>
                  <span>初审还需 {{ shadowForwardValidation.remaining_ready_pairs }}</span>
                  <span>成熟还需 {{ shadowForwardValidation.remaining_mature_pairs }}</span>
                </div>

                <el-alert
                  :title="shadowForwardStatusAlert.title"
                  :description="shadowForwardStatusAlert.description"
                  :type="shadowForwardStatusAlert.type"
                  :closable="false"
                  show-icon
                  class="shadow-evidence-alert"
                  data-testid="shadow-forward-lifecycle"
                />
                <el-alert
                  title="前向边界已锁定"
                  :description="shadowForwardRegistration
                    ? `仅纳入 ${formatForwardDateTime(shadowForwardRegistration.eligible_after)} 起始的完整目标会话；更早数据只可作为因果 seed。`
                    : '注册时间和最早纳入时间由服务端确定；注册前目标会话永不回填。'"
                  type="info"
                  :closable="false"
                  show-icon
                  class="shadow-evidence-alert"
                  data-testid="shadow-forward-no-backfill"
                />
                <el-alert
                  title="不会自动晋级或下单"
                  description="前向结果只用于人工复核，不修改影子配置、不触发现行策略，也不提交任何订单。"
                  type="info"
                  :closable="false"
                  show-icon
                  class="shadow-evidence-alert"
                  data-testid="shadow-forward-safety"
                />
                <el-alert
                  v-if="!shadowForwardRegistration && shadowForwardRegisterUnavailableReason"
                  title="暂不可冻结"
                  :description="shadowForwardRegisterUnavailableReason"
                  type="warning"
                  :closable="false"
                  show-icon
                  class="shadow-evidence-alert"
                  data-testid="shadow-forward-register-disabled"
                />

                <el-table
                  v-if="shadowForwardRegistration"
                  :data="shadowForwardDailyRows"
                  size="small"
                  empty-text="等待注册后的完整目标会话"
                  data-testid="shadow-forward-daily"
                >
                  <el-table-column type="expand" width="44">
                    <template #default="{ row }">
                      <div class="shadow-evidence-summary shadow-forward-pair-audit">
                        <span>目标开盘 {{ formatForwardDateTime(row.target_open_at) }}</span>
                        <span>评估 {{ formatForwardDateTime(row.evaluated_at) }}</span>
                        <span>目标 bar {{ row.target_bars }}</span>
                        <span>bar 哈希 {{ row.target_bars_sha256?.slice(0, 12) || '-' }}</span>
                        <span>seed 哈希 {{ row.seed_bars_sha256?.slice(0, 12) || '-' }}</span>
                        <span>
                          输入哈希 B/C
                          {{ row.baseline_input_sha256?.slice(0, 8) || '-' }} /
                          {{ row.candidate_input_sha256?.slice(0, 8) || '-' }}
                        </span>
                        <span>
                          结果哈希 B/C
                          {{ row.baseline_result_sha256?.slice(0, 8) || '-' }} /
                          {{ row.candidate_result_sha256?.slice(0, 8) || '-' }}
                        </span>
                        <span>
                          证据摘要 {{ row.evidence_digest_sha256?.slice(0, 8) || '-' }}
                        </span>
                        <span>同目标 bar {{ formatNullableBoolean(row.same_target_bars) }}</span>
                        <span>基线一致 {{ formatNullableBoolean(row.baseline_replay_match) }}</span>
                        <span>日内特征不变 {{ formatNullableBoolean(row.session_local_invariant) }}</span>
                      </div>
                      <el-alert
                        v-if="row.disposition === 'EXCLUDED'"
                        title="目标会话已排除"
                        :description="shadowForwardExclusionLabel(row.exclusion_reason)"
                        :type="row.structural_failure ? 'error' : 'warning'"
                        :closable="false"
                        show-icon
                        class="shadow-evidence-alert"
                      />
                      <el-table
                        v-if="row.included"
                        :data="shadowForwardVariantRows(row)"
                        size="small"
                        empty-text="暂无配对明细"
                      >
                        <el-table-column prop="label" label="方案" width="130" />
                        <el-table-column label="首次就绪（市场）" width="135">
                          <template #default="scope">{{ formatMarketClock(scope.row.first_ready_at) }}</template>
                        </el-table-column>
                        <el-table-column label="就绪 / 预热损失" width="125">
                          <template #default="scope">{{ scope.row.ready_bars }} / {{ scope.row.warmup_lost_bars }}</template>
                        </el-table-column>
                        <el-table-column prop="eligible_bars" label="Gate 通过" width="95" />
                        <el-table-column prop="entries" label="开仓" width="75" />
                        <el-table-column prop="closed_trades" label="闭环" width="75" />
                        <el-table-column label="净收益" width="100">
                          <template #default="scope">{{ formatNullable(scope.row.net_pnl) }}</template>
                        </el-table-column>
                        <el-table-column label="最大回撤" min-width="100">
                          <template #default="scope">{{ formatNullable(scope.row.max_drawdown) }}</template>
                        </el-table-column>
                      </el-table>
                    </template>
                  </el-table-column>
                  <el-table-column prop="target_session_date" label="目标日" width="110" />
                  <el-table-column label="资格" width="90">
                    <template #default="{ row }">
                      <el-tag :type="row.included ? 'success' : 'warning'" effect="plain">
                        {{ row.included ? '纳入' : '排除' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="seed 日" width="110">
                    <template #default="{ row }">{{ row.seed_session_date || '-' }}</template>
                  </el-table-column>
                  <el-table-column label="首次就绪 基线 / 预热" width="165">
                    <template #default="{ row }">
                      {{ formatMarketClock(row.baseline?.first_ready_at ?? null) }} /
                      {{ formatMarketClock(row.candidate?.first_ready_at ?? null) }}
                    </template>
                  </el-table-column>
                  <el-table-column label="恢复就绪" width="95">
                    <template #default="{ row }">{{ formatSignedNullableCount(row.readyDelta) }}</template>
                  </el-table-column>
                  <el-table-column label="Gate 基线 / 预热" width="135">
                    <template #default="{ row }">
                      {{ row.baseline?.eligible_bars ?? '-' }} / {{ row.candidate?.eligible_bars ?? '-' }}
                    </template>
                  </el-table-column>
                  <el-table-column label="Gate 增量" width="95">
                    <template #default="{ row }">{{ formatSignedNullableCount(row.eligibleDelta) }}</template>
                  </el-table-column>
                  <el-table-column label="闭环增量" width="95">
                    <template #default="{ row }">{{ formatSignedNullableCount(row.closedTradeDelta) }}</template>
                  </el-table-column>
                  <el-table-column label="净收益增量" width="105">
                    <template #default="{ row }">{{ formatSignedNullable(row.netPnlDelta) }}</template>
                  </el-table-column>
                  <el-table-column label="回撤变化" min-width="100">
                    <template #default="{ row }">{{ formatSignedNullable(row.drawdownDelta) }}</template>
                  </el-table-column>
                </el-table>
              </template>
            </section>

            <section v-if="shadowAdxChallengers" class="shadow-section" data-testid="shadow-adx-challengers">
              <div class="shadow-section-header">
                <div>
                  <h3>ADX 同样本对照</h3>
                  <small>
                    {{ shortVersion(shadowAdxChallengers.source_config_version) }} ·
                    即时回放不落库，永不提交订单
                  </small>
                </div>
                <div class="shadow-tags">
                  <el-tag :type="shadowAdxReplayMeta.type" effect="plain" data-testid="shadow-adx-replay">
                    {{ shadowAdxReplayMeta.label }}
                  </el-tag>
                  <el-tag :type="shadowAdxStatusMeta.type">
                    {{ shadowAdxStatusMeta.label }}
                  </el-tag>
                </div>
              </div>
              <div class="shadow-evidence-summary">
                <span>
                  完整交易日 {{ shadowAdxChallengers.evaluated_complete_sessions }} /
                  {{ shadowAdxChallengers.minimum_complete_sessions }}
                </span>
                <span>观测完整日 {{ shadowAdxChallengers.observed_complete_sessions }}</span>
                <span>方案 {{ shadowAdxChallengers.candidates.length }}</span>
              </div>
              <el-alert
                title="样本内探索：不可晋级"
                description="当前同样本结果只用于比较方案，需后续前向验证。"
                type="info"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-adx-exploratory"
              />
              <el-alert
                v-if="shadowAdxChallengers.evaluated_complete_sessions < shadowAdxChallengers.minimum_complete_sessions"
                title="完整同样本交易日不足"
                :description="`当前仅能评估 ${shadowAdxChallengers.evaluated_complete_sessions} 日，至少需要 ${shadowAdxChallengers.minimum_complete_sessions} 日。`"
                type="warning"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-adx-insufficient"
              />
              <el-alert
                v-if="shadowAdxChallengers.status === 'BLOCKED'"
                title="ADX 对照已阻塞"
                :description="shadowAdxBlockerSummary || '回放校验未通过，暂不可复核。'"
                type="error"
                :closable="false"
                show-icon
                class="shadow-evidence-alert"
                data-testid="shadow-adx-blocked"
              />
              <el-table
                :data="shadowAdxChallengers.candidates"
                size="small"
                empty-text="暂无可比方案"
                data-testid="shadow-adx-candidates"
              >
                <el-table-column type="expand">
                  <template #default="{ row }">
                    <el-table :data="row.daily" size="small" empty-text="暂无按日回放">
                      <el-table-column prop="session_date" label="交易日" width="120" />
                      <el-table-column label="bar / 有效" width="110">
                        <template #default="scope">{{ scope.row.bars }} / {{ scope.row.eligible_bars }}</template>
                      </el-table-column>
                      <el-table-column label="跌破 / 收复" width="110">
                        <template #default="scope">{{ scope.row.breaches }} / {{ scope.row.reclaims }}</template>
                      </el-table-column>
                      <el-table-column prop="closed_trades" label="闭环" width="80" />
                      <el-table-column label="净收益" width="100">
                        <template #default="scope">{{ formatNullable(scope.row.net_pnl) }}</template>
                      </el-table-column>
                      <el-table-column label="最大回撤" width="100">
                        <template #default="scope">{{ formatNullable(scope.row.max_drawdown) }}</template>
                      </el-table-column>
                      <el-table-column label="退出原因" min-width="180">
                        <template #default="scope">{{ formatExitReasons(scope.row.exit_reasons) }}</template>
                      </el-table-column>
                    </el-table>
                  </template>
                </el-table-column>
                <el-table-column label="方案" width="110">
                  <template #default="{ row }">
                    <el-tag :type="row.label === 'BASELINE' ? 'info' : 'primary'" effect="plain">
                      {{ row.label === 'BASELINE' ? '基线' : '挑战者' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="ADX 上限" width="100">
                  <template #default="{ row }">{{ row.max_adx.toFixed(1) }}</template>
                </el-table-column>
                <el-table-column label="配置版本" min-width="130">
                  <template #default="{ row }">{{ shortVersion(row.config_version) }}</template>
                </el-table-column>
                <el-table-column label="完整日" width="80">
                  <template #default="{ row }">{{ row.daily.length }}</template>
                </el-table-column>
                <el-table-column label="闭环交易" width="90">
                  <template #default="{ row }">{{ row.metrics.closed_trades }}</template>
                </el-table-column>
                <el-table-column label="净收益" width="100">
                  <template #default="{ row }">{{ formatNullable(row.metrics.net_pnl) }}</template>
                </el-table-column>
                <el-table-column label="胜率" width="90">
                  <template #default="{ row }">{{ formatPercent(row.metrics.win_rate) }}</template>
                </el-table-column>
                <el-table-column label="最大回撤" width="100">
                  <template #default="{ row }">{{ formatNullable(row.metrics.max_drawdown) }}</template>
                </el-table-column>
                <el-table-column label="有效 bar" width="90">
                  <template #default="{ row }">{{ row.metrics.eligible_bars }}</template>
                </el-table-column>
              </el-table>
            </section>

            <section class="shadow-section" data-testid="shadow-gates">
              <div class="shadow-section-header">
                <h3>Gate 统计</h3>
                <el-tag effect="plain">证据 {{ shortVersion(shadowStatus.evidence_config_version) }}</el-tag>
              </div>
              <el-table :data="shadowGateRows" size="small" empty-text="暂无 Gate 记录">
                <el-table-column prop="gate" label="Gate" min-width="180" />
                <el-table-column prop="count" label="次数" width="120" />
                <el-table-column label="占比" width="120">
                  <template #default="{ row }">{{ gateShare(row.count) }}</template>
                </el-table-column>
              </el-table>
            </section>

            <section class="shadow-section" data-testid="shadow-decisions">
              <div class="shadow-section-header">
                <h3>影子决策</h3>
                <el-button size="small" plain :disabled="shadowDecisions.length === 0" data-testid="shadow-export-decisions" @click="exportShadowDecisions">导出当前页</el-button>
              </div>
              <el-table :data="shadowDecisions" size="small" empty-text="暂无影子决策">
                <el-table-column label="时间" min-width="155">
                  <template #default="{ row }">{{ formatDateTime(row.observed_at) }}</template>
                </el-table-column>
                <el-table-column prop="action" label="动作" width="100">
                  <template #default="{ row }"><el-tag :type="shadowActionTagType(row.action)" effect="plain">{{ row.action }}</el-tag></template>
                </el-table-column>
                <el-table-column prop="reason" label="原因" min-width="210" show-overflow-tooltip />
                <el-table-column label="1m z" width="90"><template #default="{ row }">{{ formatNullable(row.zscore_1m) }}</template></el-table-column>
                <el-table-column label="5m z" width="90"><template #default="{ row }">{{ formatNullable(row.zscore_5m) }}</template></el-table-column>
                <el-table-column label="ADX" width="85"><template #default="{ row }">{{ formatNullable(row.adx) }}</template></el-table-column>
                <el-table-column label="波动率" width="100"><template #default="{ row }">{{ formatNullable(row.realized_vol, 4) }}</template></el-table-column>
                <el-table-column label="虚拟仓位" width="100"><template #default="{ row }">{{ shadowPositionLabel(row.virtual_position) }}</template></el-table-column>
                <el-table-column label="净收益" width="100"><template #default="{ row }">{{ formatNullable(row.net_pnl) }}</template></el-table-column>
              </el-table>
              <el-pagination
                v-if="shadowDecisionTotal > shadowDecisionPageSize"
                :current-page="shadowDecisionPage"
                :page-size="shadowDecisionPageSize"
                :total="shadowDecisionTotal"
                layout="prev, pager, next, total"
                class="shadow-pagination"
                data-testid="shadow-decisions-pagination"
                @current-change="loadShadowDecisions"
              />
            </section>
          </template>

          <el-empty v-else-if="!shadowLoading && !shadowLoadError" description="暂无策略 v2 配置" />
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog
      v-model="shadowForwardDialogVisible"
      title="冻结前向验证候选"
      width="500px"
      class="shadow-forward-dialog"
      :close-on-click-modal="!shadowForwardRegistering"
      :close-on-press-escape="!shadowForwardRegistering"
      :show-close="!shadowForwardRegistering"
      data-testid="shadow-forward-dialog"
      @closed="resetShadowForwardConfirmations"
    >
      <div class="shadow-forward-dialog-summary">
        <span>{{ shadowConfig?.symbol || '-' }}</span>
        <strong>{{ shortVersion(selectedShadowVersion) }}</strong>
        <span>候选</span>
        <strong>{{ shadowForwardCandidateVersion }}</strong>
      </div>
      <el-alert
        title="注册时间由服务端确定"
        description="盘前注册可从当日完整 RTH 开盘纳入；开盘时及之后注册从下一完整会话开始。seed 可早于边界，目标会话不可早于边界。"
        type="info"
        :closable="false"
        show-icon
        class="shadow-evidence-alert"
      />
      <div class="shadow-forward-confirmations">
        <el-checkbox
          v-model="shadowForwardOnlyConfirmed"
          data-testid="shadow-forward-confirm-only"
        >
          仅纳入注册边界之后的完整目标会话，不回填历史目标样本
        </el-checkbox>
        <el-checkbox
          v-model="shadowForwardNoPromotionConfirmed"
          data-testid="shadow-forward-confirm-safety"
        >
          不会自动修改配置、晋级或提交订单
        </el-checkbox>
      </div>
      <template #footer>
        <el-button
          :disabled="shadowForwardRegistering"
          data-testid="shadow-forward-register-cancel"
          @click="shadowForwardDialogVisible = false"
        >取消</el-button>
        <el-button
          type="primary"
          :loading="shadowForwardRegistering"
          :disabled="!shadowForwardOnlyConfirmed || !shadowForwardNoPromotionConfirmed"
          data-testid="shadow-forward-register-confirm"
          @click="registerShadowForwardValidation"
        >
          <el-icon><Lock /></el-icon>
          <span>冻结并开始采集</span>
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, reactive, onBeforeUnmount, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock } from '@element-plus/icons-vue'
import {
  listPromptVersions, createPromptVersion, activatePromptVersion,
  listExperimentNames, getExperimentSummary,
  getPerformanceStats, comparePerformanceVariants, getPerformanceRecommendations,
  getIndicators, getLLMUsageSummary,
} from '../api/lab'
import { getLLMInteractions, getLLMIntervalStatus } from '../api/llm_advisor'
import { getOpeningMomentumShadowStatus } from '../api/opening_momentum_shadow'
import DataState from '../components/DataState.vue'
import MetricStat from '../components/MetricStat.vue'
import {
  evaluateStrategyShadowAdxChallengers,
  getStrategyShadowConfig,
  getStrategyShadowConfigs,
  getStrategyShadowDecisions,
  getStrategyShadowEvaluation,
  getStrategyShadowForwardValidation,
  getStrategyShadowStatus,
  getStrategyShadowVersions,
  registerStrategyShadowForwardValidation,
  updateStrategyShadowConfig,
} from '../api/strategy_shadow'
import type {
  PromptVersion, ExperimentSummary, PerformanceStats,
  PerformanceVariant, IndicatorsResponse, LLMInteractionRecord, LLMIntervalStatus, LLMUsageSummary,
  OpeningMomentumShadowStatus,
  StrategyShadowAdxChallengerResponse,
  StrategyShadowConfig, StrategyShadowConfigUpdate, StrategyShadowDecision,
  StrategyShadowEvaluation, StrategyShadowForwardValidationDaily,
  StrategyShadowForwardValidationResponse, StrategyShadowStatus, StrategyShadowVersion,
  StrategyShadowWarmupVariant,
} from '../types'
import { resolveErrorMessage } from '../utils/error'
import { downloadCsv } from '../utils/csv'

const activeTab = ref('experiments')

// --- Tab 1: versions & experiments ---
const versions = ref<PromptVersion[]>([])
const newVersion = reactive({ name: '', version: '', description: '', template: '' })
const creating = ref(false)
const experimentNames = ref<string[]>([])
const selectedSummaryExp = ref('')
const summary = ref<ExperimentSummary[]>([])

async function loadVersions() {
  try {
    versions.value = await listPromptVersions()
  } catch {
    ElMessage.error('加载版本失败')
  }
}
async function loadExperimentNames() {
  try {
    experimentNames.value = await listExperimentNames()
  } catch {
    ElMessage.error('加载实验列表失败')
  }
}
async function submitVersion() {
  if (!newVersion.name || !newVersion.version || !newVersion.template) {
    ElMessage.warning('name / version / template 必填')
    return
  }
  creating.value = true
  try {
    await createPromptVersion({ ...newVersion })
    ElMessage.success('版本已创建')
    newVersion.name = ''; newVersion.version = ''; newVersion.description = ''; newVersion.template = ''
    await loadVersions()
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '创建失败'))
  } finally {
    creating.value = false
  }
}
async function activate(v: PromptVersion) {
  try {
    await ElMessageBox.confirm(`确认将 "${v.name} ${v.version}" 设为激活版本？`, '确认激活')
  } catch {
    return
  }
  try {
    await activatePromptVersion(v.id)
    ElMessage.success('已激活')
    await loadVersions()
  } catch {
    ElMessage.error('激活失败')
  }
}
async function loadSummary() {
  if (!selectedSummaryExp.value) return
  try {
    summary.value = await getExperimentSummary(selectedSummaryExp.value)
  } catch {
    ElMessage.error('加载实验摘要失败')
  }
}

// --- Tab 2: performance ---
const perfExp = ref('')
const stats = ref<PerformanceStats | null>(null)
const variants = ref<PerformanceVariant[]>([])
const recommendations = ref<string[]>([])

async function loadPerformance() {
  if (!perfExp.value) { stats.value = null; variants.value = []; recommendations.value = []; return }
  try {
    const [s, c, r] = await Promise.all([
      getPerformanceStats(perfExp.value),
      comparePerformanceVariants(perfExp.value),
      getPerformanceRecommendations(perfExp.value),
    ])
    stats.value = s; variants.value = c; recommendations.value = r
  } catch {
    ElMessage.error('加载性能数据失败')
  }
}

// --- Tab 3: indicators ---
const indicatorSymbol = ref('')
const indicators = ref<IndicatorsResponse | null>(null)
const indicatorsLoading = ref(false)

async function loadIndicators() {
  indicatorsLoading.value = true
  try {
    indicators.value = await getIndicators(indicatorSymbol.value || undefined)
    indicatorSymbol.value = indicators.value.symbol
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '指标加载失败'))
  } finally {
    indicatorsLoading.value = false
  }
}

function pct(v: number): string { return `${(v * 100).toFixed(1)}%` }

// --- Tab 4: LLM runtime observability ---
const runtimeStatus = ref<LLMIntervalStatus | null>(null)
const runtimeInteractions = ref<LLMInteractionRecord[]>([])
const runtimeLoading = ref(false)
const runtimeLoaded = ref(false)
const usageDays = ref(30)
const usageSummary = ref<LLMUsageSummary | null>(null)
const usageLoading = ref(false)
const usageError = ref('')

const runtimeHealthHints = computed(() => {
  const status = runtimeStatus.value
  if (!status) return []
  const hints: string[] = []
  if (!status.enabled) hints.push('LLM 区间分析未启用')
  if (status.budget.remaining_analyses_this_hour <= 0) hints.push('预算已耗尽')
  if (!status.next_analysis_at) hints.push('暂无下一次分析时间')
  for (const row of status.symbol_statuses) {
    if (row.last_skip_reason) hints.push(`${row.symbol}: ${row.last_skip_reason}`)
    if (row.has_pending_order) hints.push(`${row.symbol}: 存在挂单，可能跳过分析或交易动作`)
  }
  return hints
})

const budgetUsagePct = computed(() => {
  const b = runtimeStatus.value?.budget
  if (!b || !b.max_analyses_per_hour) return 0
  return Math.min(100, Math.round((b.used_analyses_last_hour / b.max_analyses_per_hour) * 100))
})

async function loadRuntimeStatus() {
  void loadLLMUsageSummary()
  runtimeLoading.value = true
  try {
    const [status, interactions] = await Promise.all([
      getLLMIntervalStatus(),
      getLLMInteractions(20),
    ])
    runtimeStatus.value = status
    runtimeInteractions.value = interactions
    runtimeLoaded.value = true
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '加载 LLM 运行状态失败'))
  } finally {
    runtimeLoading.value = false
  }
}

async function loadLLMUsageSummary() {
  usageLoading.value = true
  usageError.value = ''
  try {
    usageSummary.value = await getLLMUsageSummary(usageDays.value)
  } catch (e: unknown) {
    usageSummary.value = null
    usageError.value = resolveErrorMessage(e, '加载大模型用量失败')
  } finally {
    usageLoading.value = false
  }
}

function exportSymbolStatus() {
  const rows = (runtimeStatus.value?.symbol_statuses ?? []).map((s) => ({
    symbol: s.symbol,
    market: s.market,
    is_primary: s.is_primary ? 'yes' : 'no',
    has_pending_order: s.has_pending_order ? 'yes' : 'no',
    last_analysis_at: s.last_analysis_at ?? '',
    next_analysis_at: s.next_analysis_at ?? '',
    last_status: s.last_status ?? '',
    last_skip_reason: s.last_skip_reason ?? '',
  }))
  downloadCsv('llm_symbol_status.csv', [
    { key: 'symbol', label: 'symbol' },
    { key: 'market', label: 'market' },
    { key: 'is_primary', label: 'is_primary' },
    { key: 'has_pending_order', label: 'has_pending_order' },
    { key: 'last_analysis_at', label: 'last_analysis_at' },
    { key: 'next_analysis_at', label: 'next_analysis_at' },
    { key: 'last_status', label: 'last_status' },
    { key: 'last_skip_reason', label: 'last_skip_reason' },
  ], rows)
  ElMessage.success('已导出 Symbol 状态')
}

function exportInteractions() {
  const rows = runtimeInteractions.value.map((r) => ({
    id: r.id,
    interaction_type: r.interaction_type,
    symbol: r.symbol,
    success: r.success ? 'yes' : 'no',
    applied: r.applied ? 'yes' : 'no',
    order_action: r.order_action ?? '',
    order_status: r.order_status ?? '',
    error: r.error ?? '',
    created_at: r.created_at,
  }))
  downloadCsv('llm_interactions.csv', [
    { key: 'id', label: 'id' },
    { key: 'interaction_type', label: 'interaction_type' },
    { key: 'symbol', label: 'symbol' },
    { key: 'success', label: 'success' },
    { key: 'applied', label: 'applied' },
    { key: 'order_action', label: 'order_action' },
    { key: 'order_status', label: 'order_status' },
    { key: 'error', label: 'error' },
    { key: 'created_at', label: 'created_at' },
  ], rows)
  ElMessage.success(`已导出 ${rows.length} 条交互`)
}

function formatDateTime(value: string | null): string {
  if (!value) return '-'
  return new Date(value).toLocaleString([], {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

function formatTokenCount(value: number): string {
  return value.toLocaleString()
}

// --- Tab 5: strategy v2 shadow observability ---
const openingMomentumStatus = ref<OpeningMomentumShadowStatus | null>(null)
const openingMomentumLoadError = ref('')
const shadowConfig = ref<StrategyShadowConfig | null>(null)
const shadowConfigs = ref<StrategyShadowConfig[]>([])
const selectedShadowSymbol = ref('')
const shadowStatus = ref<StrategyShadowStatus | null>(null)
const shadowVersions = ref<StrategyShadowVersion[]>([])
const selectedShadowVersion = ref('')
const shadowEvaluation = ref<StrategyShadowEvaluation | null>(null)
const shadowAdxChallengers = ref<StrategyShadowAdxChallengerResponse | null>(null)
const shadowAdxChallengerError = ref('')
const shadowAdxLoading = ref(false)
const shadowForwardValidation = ref<StrategyShadowForwardValidationResponse | null>(null)
const shadowForwardLoading = ref(false)
const shadowForwardError = ref('')
const shadowForwardRegistering = ref(false)
const shadowForwardDialogVisible = ref(false)
const shadowForwardOnlyConfirmed = ref(false)
const shadowForwardNoPromotionConfirmed = ref(false)
const shadowDecisions = ref<StrategyShadowDecision[]>([])
const shadowDecisionTotal = ref(0)
const shadowDecisionPage = ref(1)
const shadowDecisionPageSize = 20
const shadowLoading = ref(false)
const shadowSaving = ref(false)
const shadowLoaded = ref(false)
const shadowLoadError = ref('')
const shadowRequestGeneration = ref(0)
const shadowDecisionRequestGeneration = ref(0)
const shadowStatusFetchedAtMs = ref(0)
const shadowNowMs = ref(Date.now())
const shadowForwardCandidateVersion = 'strategy-v2-causal-trend-prewarm-v1' as const
const openingMomentumStateLabel = computed(() => {
  const labels: Record<OpeningMomentumShadowStatus['state'], string> = {
    DISABLED: '已停用',
    WAITING: '等待开盘',
    OPEN: '虚拟持仓',
    COLLECTING: '采集中',
  }
  return openingMomentumStatus.value
    ? labels[openingMomentumStatus.value.state]
    : '加载中'
})
const openingMomentumStateType = computed(() => {
  const state = openingMomentumStatus.value?.state
  if (state === 'OPEN') return 'warning'
  if (state === 'COLLECTING') return 'success'
  return 'info'
})
function openingMomentumVariantLabel(
  variant: OpeningMomentumShadowStatus['variants'][number]['variant'],
): string {
  return variant === 'INCUMBENT' ? '现行选池' : '动量延续'
}
const shadowForm = reactive<StrategyShadowConfigUpdate>({
  enabled: false,
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
  profit_target_pct: 0.5,
})
const shadowMax5mWindow = computed(() => shadowConfig.value?.symbol.endsWith('.HK') ? 56 : 68)
const shadowMaxAdxPeriod = computed(() => shadowConfig.value?.symbol.endsWith('.HK') ? 28 : 34)
const shadowVersionTransitionTitle = computed(() => {
  const status = shadowStatus.value
  if (!status?.version_transition_pending) return ''
  const currentConfig = status.config
  const disabledSuffix = currentConfig.enabled ? '' : '（采集仍停用）'
  if (status.evidence_config_version !== currentConfig.config_version) {
    return `版本切换等待中：旧虚拟仓位仍由 ${shortVersion(status.evidence_config_version)} 收尾；平仓后切换到 ${shortVersion(currentConfig.config_version)}${disabledSuffix}`
  }
  return `版本切换等待中：运行状态尚待初始化 ${shortVersion(currentConfig.config_version)}${disabledSuffix}`
})

function applyShadowConfig(config: StrategyShadowConfig) {
  shadowConfig.value = config
  Object.assign(shadowForm, {
    enabled: config.enabled,
    zscore_window_1m_bars: config.zscore_window_1m_bars,
    zscore_window_5m_bars: config.zscore_window_5m_bars,
    breach_zscore: config.breach_zscore,
    reclaim_zscore: config.reclaim_zscore,
    five_minute_zscore_max: config.five_minute_zscore_max,
    adx_period: config.adx_period,
    max_adx: config.max_adx,
    realized_vol_window_bars: config.realized_vol_window_bars,
    min_realized_vol: config.min_realized_vol,
    max_realized_vol: config.max_realized_vol,
    stop_loss_pct: config.stop_loss_pct,
    profit_target_pct: config.profit_target_pct,
  })
}

async function loadOpeningMomentumShadow() {
  openingMomentumLoadError.value = ''
  try {
    openingMomentumStatus.value = await getOpeningMomentumShadowStatus()
  } catch (error: unknown) {
    openingMomentumLoadError.value = resolveErrorMessage(
      error,
      '加载开盘横截面动量状态失败',
    )
  }
}

async function loadStrategyShadow(symbol = selectedShadowSymbol.value || undefined) {
  void loadOpeningMomentumShadow()
  const generation = ++shadowRequestGeneration.value
  shadowDecisionRequestGeneration.value += 1
  shadowLoading.value = true
  shadowLoadError.value = ''
  shadowConfig.value = null
  shadowStatus.value = null
  shadowVersions.value = []
  selectedShadowVersion.value = ''
  shadowEvaluation.value = null
  shadowDecisions.value = []
  shadowDecisionTotal.value = 0
  shadowDecisionPage.value = 1
  shadowAdxChallengers.value = null
  shadowAdxChallengerError.value = ''
  shadowAdxLoading.value = false
  shadowForwardValidation.value = null
  shadowForwardError.value = ''
  shadowForwardLoading.value = false
  shadowStatusFetchedAtMs.value = 0
  shadowLoaded.value = false
  try {
    const configs = await getStrategyShadowConfigs()
    if (generation !== shadowRequestGeneration.value) return
    shadowConfigs.value = configs
    const requestedSymbol = symbol || configs[0]?.symbol
    const config = await getStrategyShadowConfig(requestedSymbol)
    const versions = await getStrategyShadowVersions(config.symbol)
    const currentVersion = versions.find((item) => item.current)?.config_version ?? config.config_version
    const [status, decisions, evaluation] = await Promise.all([
      getStrategyShadowStatus(config.symbol),
      getStrategyShadowDecisions({
        symbol: config.symbol,
        config_version: currentVersion,
        page: 1,
        page_size: shadowDecisionPageSize,
      }),
      getStrategyShadowEvaluation(config.symbol, currentVersion),
    ])
    if (generation !== shadowRequestGeneration.value) return
    applyShadowConfig(config)
    selectedShadowSymbol.value = config.symbol
    shadowStatus.value = status
    shadowStatusFetchedAtMs.value = Date.now()
    shadowVersions.value = versions
    selectedShadowVersion.value = currentVersion
    shadowEvaluation.value = evaluation
    shadowDecisions.value = decisions.items
    shadowDecisionTotal.value = decisions.total
    shadowDecisionPage.value = decisions.page
    shadowLoaded.value = true
    void loadShadowAdxChallengers(config.symbol, currentVersion, generation)
    void loadShadowForwardValidation(config.symbol, currentVersion, generation)
  } catch (error: unknown) {
    if (generation !== shadowRequestGeneration.value) return
    shadowLoadError.value = resolveErrorMessage(error, '加载策略 v2 影子状态失败')
    shadowLoaded.value = false
  } finally {
    if (generation === shadowRequestGeneration.value) shadowLoading.value = false
  }
}

async function loadShadowEvidence() {
  const symbol = shadowConfig.value?.symbol
  if (!symbol || !selectedShadowVersion.value) return
  const version = selectedShadowVersion.value
  const generation = ++shadowRequestGeneration.value
  shadowDecisionRequestGeneration.value += 1
  shadowEvaluation.value = null
  shadowDecisions.value = []
  shadowDecisionTotal.value = 0
  shadowDecisionPage.value = 1
  shadowAdxChallengers.value = null
  shadowAdxChallengerError.value = ''
  shadowAdxLoading.value = false
  shadowForwardValidation.value = null
  shadowForwardError.value = ''
  shadowForwardLoading.value = false
  try {
    const [evaluation, decisions] = await Promise.all([
      getStrategyShadowEvaluation(symbol, version),
      getStrategyShadowDecisions({
        symbol,
        config_version: version,
        page: 1,
        page_size: shadowDecisionPageSize,
      }),
    ])
    if (
      generation !== shadowRequestGeneration.value
      || selectedShadowSymbol.value !== symbol
      || selectedShadowVersion.value !== version
    ) return
    shadowEvaluation.value = evaluation
    shadowDecisions.value = decisions.items
    shadowDecisionTotal.value = decisions.total
    shadowDecisionPage.value = decisions.page
    void loadShadowAdxChallengers(symbol, version, generation)
    void loadShadowForwardValidation(symbol, version, generation)
  } catch (error: unknown) {
    if (
      generation !== shadowRequestGeneration.value
      || selectedShadowSymbol.value !== symbol
      || selectedShadowVersion.value !== version
    ) return
    ElMessage.error(resolveErrorMessage(error, '加载证据版本失败'))
  }
}

async function loadShadowAdxChallengers(
  symbol: string,
  version: string,
  generation: number,
) {
  if (
    generation !== shadowRequestGeneration.value
    || selectedShadowSymbol.value !== symbol
    || selectedShadowVersion.value !== version
  ) return
  shadowAdxLoading.value = true
  try {
    const result = await evaluateStrategyShadowAdxChallengers({
      symbol,
      config_version: version,
    })
    if (
      generation !== shadowRequestGeneration.value
      || selectedShadowSymbol.value !== symbol
      || selectedShadowVersion.value !== version
    ) return
    shadowAdxChallengers.value = result
    shadowAdxChallengerError.value = ''
  } catch (error: unknown) {
    if (
      generation !== shadowRequestGeneration.value
      || selectedShadowSymbol.value !== symbol
      || selectedShadowVersion.value !== version
    ) return
    shadowAdxChallengers.value = null
    shadowAdxChallengerError.value = resolveErrorMessage(
      error,
      '加载 ADX 同样本对照失败',
    )
  } finally {
    if (
      generation === shadowRequestGeneration.value
      && selectedShadowSymbol.value === symbol
      && selectedShadowVersion.value === version
    ) shadowAdxLoading.value = false
  }
}

async function loadShadowForwardValidation(
  symbol: string,
  version: string,
  generation: number,
) {
  if (
    generation !== shadowRequestGeneration.value
    || selectedShadowSymbol.value !== symbol
    || selectedShadowVersion.value !== version
  ) return
  shadowForwardLoading.value = true
  try {
    const result = await getStrategyShadowForwardValidation(symbol)
    if (
      generation !== shadowRequestGeneration.value
      || selectedShadowSymbol.value !== symbol
      || selectedShadowVersion.value !== version
    ) return
    shadowForwardValidation.value = result
    shadowForwardError.value = ''
  } catch (error: unknown) {
    if (
      generation !== shadowRequestGeneration.value
      || selectedShadowSymbol.value !== symbol
      || selectedShadowVersion.value !== version
    ) return
    shadowForwardValidation.value = null
    shadowForwardError.value = resolveErrorMessage(
      error,
      '加载前向验证失败',
    )
  } finally {
    if (
      generation === shadowRequestGeneration.value
      && selectedShadowSymbol.value === symbol
      && selectedShadowVersion.value === version
    ) shadowForwardLoading.value = false
  }
}

async function loadShadowDecisions(page: number) {
  const symbol = shadowConfig.value?.symbol
  const version = selectedShadowVersion.value
  const generation = shadowRequestGeneration.value
  const decisionGeneration = ++shadowDecisionRequestGeneration.value
  if (!symbol || !version) return
  try {
    const result = await getStrategyShadowDecisions({
      symbol,
      config_version: version,
      page,
      page_size: shadowDecisionPageSize,
    })
    if (
      generation !== shadowRequestGeneration.value
      || decisionGeneration !== shadowDecisionRequestGeneration.value
      || shadowConfig.value?.symbol !== symbol
      || selectedShadowVersion.value !== version
    ) return
    shadowDecisions.value = result.items
    shadowDecisionTotal.value = result.total
    shadowDecisionPage.value = result.page
  } catch (error: unknown) {
    if (
      generation !== shadowRequestGeneration.value
      || decisionGeneration !== shadowDecisionRequestGeneration.value
      || shadowConfig.value?.symbol !== symbol
      || selectedShadowVersion.value !== version
    ) return
    ElMessage.error(resolveErrorMessage(error, '加载影子决策失败'))
  }
}

function validateShadowForm(): string | null {
  if (shadowForm.breach_zscore >= shadowForm.reclaim_zscore) {
    return '跌破阈值必须小于收复阈值'
  }
  if (shadowForm.min_realized_vol >= shadowForm.max_realized_vol) {
    return '波动率下限必须小于上限'
  }
  if (shadowForm.zscore_window_5m_bars > shadowMax5mWindow.value) {
    return `5m z-score 窗口不能超过 ${shadowMax5mWindow.value}`
  }
  if (shadowForm.adx_period > shadowMaxAdxPeriod.value) {
    return `ADX 周期不能超过 ${shadowMaxAdxPeriod.value}`
  }
  return null
}

async function saveShadowConfig() {
  const validationError = validateShadowForm()
  if (validationError) {
    ElMessage.warning(validationError)
    return
  }
  try {
    await ElMessageBox.confirm('确认更新策略 v2 的影子采集配置？', '确认保存')
  } catch {
    return
  }
  shadowSaving.value = true
  try {
    const config = await updateStrategyShadowConfig(
      { ...shadowForm },
      shadowConfig.value?.symbol,
    )
    applyShadowConfig(config)
    ElMessage.success('影子配置已更新')
    await loadStrategyShadow()
  } catch (error: unknown) {
    ElMessage.error(resolveErrorMessage(error, '影子配置保存失败'))
  } finally {
    shadowSaving.value = false
  }
}

const shadowGateRows = computed(() => {
  const counts = shadowStatus.value?.gate_counts ?? {}
  return Object.entries(counts)
    .map(([gate, count]) => ({ gate, count }))
    .sort((a, b) => b.count - a.count || a.gate.localeCompare(b.gate))
})

const shadowFreshnessLabel = computed(() => {
  const serverAge = shadowStatus.value?.latest?.data_age_seconds
  const age = serverAge == null
    ? null
    : serverAge + Math.max(0, shadowNowMs.value - shadowStatusFetchedAtMs.value) / 1000
  if (age == null) return '无数据'
  if (age < 1) return '刚刚更新'
  return `${Math.round(age)} 秒前`
})

const shadowFreshnessType = computed<'success' | 'warning' | 'danger'>(() => {
  const serverAge = shadowStatus.value?.latest?.data_age_seconds
  const age = serverAge == null
    ? Number.POSITIVE_INFINITY
    : serverAge + Math.max(0, shadowNowMs.value - shadowStatusFetchedAtMs.value) / 1000
  if (age <= 15) return 'success'
  if (age <= 60) return 'warning'
  return 'danger'
})

const shadowDayProgress = computed(() => {
  const value = shadowEvaluation.value
  return value ? Math.min(100, Math.round(value.observed_trading_days / value.minimum_trading_days * 100)) : 0
})

const shadowTradeProgress = computed(() => {
  const value = shadowEvaluation.value
  return value ? Math.min(100, Math.round(value.eligible_closed_trades / value.minimum_closed_trades * 100)) : 0
})

const shadowBlockerLabels: Record<string, string> = {
  MIN_TRADING_DAYS: '完整交易日不足',
  MIN_CLOSED_TRADES: '完整会话闭环交易不足',
  DATA_TRADE_EVIDENCE_INVALID: '交易与决策证据无法关联',
  DATA_TRADE_SESSION_INCOMPLETE: '残缺会话包含交易',
  NET_PNL_NON_POSITIVE: '净收益尚未为正',
  COST_STRESS_NET_PNL_NON_POSITIVE: '费用压力后净收益不为正',
  COST_STRESS_UNAVAILABLE: '费用压力证据不可用',
  MAX_DRAWDOWN_EXCEEDS_NET_PNL: '最大回撤超过净收益',
  QUALITY_DATA_INCOMPLETE: '交易收益证据不完整',
  HK_MIN_NET_EDGE: '港股目标收益不足以覆盖成本',
  CONFIG_COST_SNAPSHOT_INCOMPLETE: '成本配置证据不完整',
}

const shadowBlockerSummary = computed(() => (
  shadowEvaluation.value?.readiness_blockers
    .map((item) => shadowBlockerLabels[item] ?? item)
    .join('；') ?? ''
))

const shadowAdxStatusMeta = computed<{
  label: string
  type: 'success' | 'warning' | 'danger'
}>(() => {
  if (shadowAdxChallengers.value?.status === 'READY_FOR_REVIEW') {
    return { label: '可复核', type: 'success' }
  }
  if (shadowAdxChallengers.value?.status === 'BLOCKED') {
    return { label: '已阻塞', type: 'danger' }
  }
  return { label: '证据不足', type: 'warning' }
})

const shadowAdxReplayMeta = computed<{
  label: string
  type: 'success' | 'danger' | 'info'
}>(() => {
  if (shadowAdxChallengers.value?.baseline_replay_match === true) {
    return { label: '基线复放一致', type: 'success' }
  }
  if (shadowAdxChallengers.value?.baseline_replay_match === false) {
    return { label: '基线复放不一致', type: 'danger' }
  }
  return { label: '基线尚不可校验', type: 'info' }
})

const shadowAdxBlockerLabels: Record<string, string> = {
  MIN_COMPLETE_SESSIONS: '完整同样本交易日不足',
  BASELINE_REPLAY_MISMATCH: '基线回放与持久化指标不一致',
  ALGORITHM_VERSION_UNSUPPORTED: '算法版本不支持同样本回放',
  CONFIG_SNAPSHOT_INVALID: '源配置快照不完整',
  CONFIG_SNAPSHOT_VERSION_MISMATCH: '源配置快照版本校验失败',
}

const shadowAdxBlockerSummary = computed(() => (
  shadowAdxChallengers.value?.blockers
    .map((item) => shadowAdxBlockerLabels[item] ?? item)
    .join('；') ?? ''
))

const shadowWarmupDiagnostic = computed(() => (
  shadowAdxChallengers.value?.warmup_diagnostic ?? null
))

const shadowWarmupStatusMeta = computed<{
  label: string
  type: 'success' | 'warning' | 'danger'
}>(() => {
  if (shadowWarmupDiagnostic.value?.status === 'READY_FOR_REVIEW') {
    return { label: '可复核', type: 'success' }
  }
  if (shadowWarmupDiagnostic.value?.status === 'BLOCKED') {
    return { label: '已阻塞', type: 'danger' }
  }
  return { label: '证据不足', type: 'warning' }
})

const shadowWarmupBlockerLabels: Record<string, string> = {
  MIN_CAUSAL_PAIRS: '因果配对完整交易日不足',
  BASELINE_REPLAY_MISMATCH: '基线回放与持久化证据不一致',
  ALGORITHM_VERSION_UNSUPPORTED: '算法版本不支持因果预热诊断',
  CONFIG_SNAPSHOT_INVALID: '源配置快照不完整',
  CONFIG_SNAPSHOT_VERSION_MISMATCH: '源配置快照版本校验失败',
  SESSION_LOCAL_FEATURE_DRIFT: '日内 VWAP / z-score 与基线发生偏移',
  PREWARM_REPLAY_FAILED: '因果预热回放校验失败',
}

const shadowWarmupBlockerSummary = computed(() => (
  shadowWarmupDiagnostic.value?.blockers
    .map((item) => shadowWarmupBlockerLabels[item] ?? item)
    .join('；') ?? ''
))

function sumWarmupDaily(
  variant: StrategyShadowWarmupVariant,
  field: 'bars' | 'ready_bars' | 'warmup_lost_bars' | 'eligible_bars',
): number {
  return variant.daily.reduce((total, item) => total + item[field], 0)
}

const shadowWarmupVariantRows = computed(() => {
  const variants = shadowWarmupDiagnostic.value?.variants ?? []
  const baseline = variants.find((item) => item.label === 'SESSION_LOCAL')
  const baselineReady = baseline ? sumWarmupDaily(baseline, 'ready_bars') : 0
  const baselineEligible = baseline ? sumWarmupDaily(baseline, 'eligible_bars') : 0
  return variants.map((item) => {
    const readyBars = sumWarmupDaily(item, 'ready_bars')
    const eligibleBars = sumWarmupDaily(item, 'eligible_bars')
    return {
      ...item,
      bars: sumWarmupDaily(item, 'bars'),
      readyBars,
      warmupLostBars: sumWarmupDaily(item, 'warmup_lost_bars'),
      eligibleBars,
      recoveredReadyBars: item.label === 'SESSION_LOCAL' ? 0 : readyBars - baselineReady,
      eligibleDelta: item.label === 'SESSION_LOCAL' ? 0 : eligibleBars - baselineEligible,
    }
  })
})

interface ShadowWarmupHourlyAggregate {
  bars: number
  readyBars: number
  eligibleBars: number
  gateCounts: Record<string, number>
}

function aggregateWarmupHourly(
  variant: StrategyShadowWarmupVariant | undefined,
): Map<number, ShadowWarmupHourlyAggregate> {
  const result = new Map<number, ShadowWarmupHourlyAggregate>()
  for (const daily of variant?.daily ?? []) {
    for (const item of daily.hourly_eligibility) {
      const current = result.get(item.session_hour) ?? {
        bars: 0,
        readyBars: 0,
        eligibleBars: 0,
        gateCounts: {},
      }
      current.bars += item.bars
      current.readyBars += item.ready_bars
      current.eligibleBars += item.eligible_bars
      for (const [gate, count] of Object.entries(item.gate_counts)) {
        current.gateCounts[gate] = (current.gateCounts[gate] ?? 0) + count
      }
      result.set(item.session_hour, current)
    }
  }
  return result
}

const shadowWarmupHourlyRows = computed(() => {
  const variants = shadowWarmupDiagnostic.value?.variants ?? []
  const baseline = aggregateWarmupHourly(
    variants.find((item) => item.label === 'SESSION_LOCAL'),
  )
  const prewarm = aggregateWarmupHourly(
    variants.find((item) => item.label === 'CAUSAL_TREND_PREWARM'),
  )
  const hours = [...new Set([...baseline.keys(), ...prewarm.keys()])]
    .sort((left, right) => left - right)
  return hours.map((sessionHour) => {
    const baselineItem = baseline.get(sessionHour)
    const prewarmItem = prewarm.get(sessionHour)
    const baselineReady = baselineItem?.readyBars ?? 0
    const prewarmReady = prewarmItem?.readyBars ?? 0
    const baselineEligible = baselineItem?.eligibleBars ?? 0
    const prewarmEligible = prewarmItem?.eligibleBars ?? 0
    return {
      sessionHour,
      sessionLabel: formatShadowSessionHour(sessionHour),
      baselineBars: baselineItem?.bars ?? 0,
      prewarmBars: prewarmItem?.bars ?? 0,
      baselineReady,
      prewarmReady,
      baselineEligible,
      prewarmEligible,
      readyDelta: prewarmReady - baselineReady,
      eligibleDelta: prewarmEligible - baselineEligible,
      prewarmGateCounts: prewarmItem?.gateCounts ?? {},
    }
  })
})

const shadowForwardRegistration = computed(() => (
  shadowForwardValidation.value?.registration ?? null
))

const shadowForwardStatusMeta = computed<{
  label: string
  type: 'success' | 'warning' | 'danger' | 'info'
}>(() => {
  switch (shadowForwardValidation.value?.status) {
    case 'FROZEN':
      return { label: '已冻结', type: 'info' }
    case 'COLLECTING':
      return { label: '前向采集中', type: 'warning' }
    case 'READY_FOR_REVIEW':
      return { label: '可复核', type: 'success' }
    case 'MATURE_EVIDENCE':
      return { label: '证据成熟', type: 'success' }
    case 'BLOCKED':
      return { label: '已阻塞', type: 'danger' }
    default:
      return { label: '尚未注册', type: 'info' }
  }
})

const shadowForwardStatusAlert = computed<{
  title: string
  description: string
  type: 'success' | 'warning' | 'error' | 'info'
}>(() => {
  const value = shadowForwardValidation.value
  const registration = value?.registration
  switch (value?.status) {
    case 'FROZEN':
      return {
        title: '候选已冻结',
        description: `等待自 ${formatForwardDateTime(registration?.eligible_after ?? null)} 起的首个完整目标会话。`,
        type: 'info',
      }
    case 'COLLECTING':
      return {
        title: '注册后样本采集中',
        description: `已纳入 ${value.included_pairs} 对，距离初步复核还需 ${value.remaining_ready_pairs} 对。`,
        type: 'warning',
      }
    case 'READY_FOR_REVIEW':
      return {
        title: '样本已达初步复核门槛',
        description: `当前 ${value.included_pairs} 对仅可人工复核，不代表候选胜出或可以晋级。`,
        type: 'success',
      }
    case 'MATURE_EVIDENCE':
      return {
        title: '前向证据已成熟',
        description: `当前 ${value.included_pairs} 对已达到成熟门槛，仍不会自动修改配置或晋级。`,
        type: 'success',
      }
    case 'BLOCKED':
      return {
        title: '前向验证已阻塞',
        description: shadowForwardBlockerSummary.value || '完整性校验未通过，冻结 cohort 不再接收证据。',
        type: 'error',
      }
    default:
      return {
        title: '尚未冻结前向候选',
        description: '注册后才开始计入目标会话；注册前数据不会回填为前向样本。',
        type: 'info',
      }
  }
})

const shadowForwardBlockerLabels: Record<string, string> = {
  BASELINE_REPLAY_MISMATCH: '基线回放与持久化证据不一致',
  EVALUATOR_DEFINITION_MISMATCH: '评估器定义与注册时不一致',
  EVIDENCE_DIGEST_MISMATCH: '前向证据摘要校验失败',
  EVIDENCE_DISPOSITION_INVALID: '前向证据资格字段无效',
  EVIDENCE_EXCLUSION_INVALID: '排除证据语义校验失败',
  EVIDENCE_PAYLOAD_INVALID: '前向证据载荷无效',
  EVIDENCE_TARGET_BOUNDARY_INVALID: '目标会话早于注册边界',
  FORWARD_EVALUATION_FAILED: '前向评估失败',
  REGISTRATION_BOUNDARY_INVALID: '注册纳入边界无效',
  REGISTRATION_METADATA_INVALID: '注册元数据校验失败',
  SESSION_LOCAL_FEATURE_DRIFT: '日内 VWAP / z-score 与基线发生偏移',
  SOURCE_VERSION_SUPERSEDED: '冻结源版本已被替换',
  TARGET_EVIDENCE_NOT_KNOWN_AT_EVALUATION: '目标证据在评估时尚不可用',
  TARGET_INPUT_HASH_MISMATCH: '基线与候选目标输入不一致',
  TARGET_STATE_NOT_FLAT: '目标交易日结束时影子状态仍有持仓',
}

const shadowForwardBlockerSummary = computed(() => (
  shadowForwardValidation.value?.blockers
    .map((item) => shadowForwardBlockerLabels[item] ?? item)
    .join('；') ?? ''
))

const shadowForwardRegisterUnavailableReason = computed(() => {
  if (shadowForwardLoading.value) return '正在读取前向验证状态'
  if (!shadowForwardValidation.value) return shadowForwardError.value || '前向验证状态不可用'
  if (shadowForwardRegistration.value) return ''
  if (!shadowConfig.value?.enabled) return '请先启用当前影子采集配置'
  if (shadowStatus.value?.version_transition_pending) return '版本过渡完成后才能冻结候选'
  if (
    selectedShadowVersion.value !== shadowConfig.value.config_version
    || selectedShadowVersion.value !== shadowStatus.value?.evidence_config_version
  ) return '只能为当前稳定证据版本冻结候选'
  if (!shadowWarmupDiagnostic.value) return '正在等待因果预热完整性校验'
  if (shadowWarmupDiagnostic.value.status === 'BLOCKED') return '因果预热诊断已阻塞，不能注册'
  return ''
})

const shadowForwardCanRegister = computed(() => (
  !shadowForwardRegistration.value && !shadowForwardRegisterUnavailableReason.value
))

const shadowForwardDailyRows = computed(() => (
  (shadowForwardValidation.value?.daily ?? []).map((item) => {
    const baseline = item.baseline
    const candidate = item.candidate
    const baselineMetrics = item.baseline_metrics
    const candidateMetrics = item.candidate_metrics
    const included = item.disposition === 'INCLUDED'
      && baseline !== null
      && candidate !== null
      && baselineMetrics !== null
      && candidateMetrics !== null
    return {
      ...item,
      included,
      readyDelta: included ? candidate.ready_bars - baseline.ready_bars : null,
      warmupLostDelta: included ? candidate.warmup_lost_bars - baseline.warmup_lost_bars : null,
      eligibleDelta: included ? candidate.eligible_bars - baseline.eligible_bars : null,
      entryDelta: included ? candidateMetrics.entries - baselineMetrics.entries : null,
      closedTradeDelta: included
        ? candidateMetrics.closed_trades - baselineMetrics.closed_trades
        : null,
      netPnlDelta: included ? candidateMetrics.net_pnl - baselineMetrics.net_pnl : null,
      drawdownDelta: included
        ? candidateMetrics.max_drawdown - baselineMetrics.max_drawdown
        : null,
    }
  })
))

function shadowForwardVariantRows(row: StrategyShadowForwardValidationDaily) {
  const values = [
    { label: '日内冷启动', daily: row.baseline, metrics: row.baseline_metrics },
    { label: '因果趋势预热', daily: row.candidate, metrics: row.candidate_metrics },
  ]
  return values.flatMap((item) => (
    item.daily && item.metrics ? [{ ...item.daily, ...item.metrics, label: item.label }] : []
  ))
}

const shadowForwardExclusionLabels: Record<string, string> = {
  BASELINE_REPLAY_MISMATCH: '基线复放不一致',
  COLLECTION_DISABLED: '采集已停用',
  EVALUATOR_DEFINITION_MISMATCH: '评估器定义发生变化',
  FINALIZATION_WINDOW_MISSED: '错过固定收盘物化窗口',
  FORWARD_EVALUATION_FAILED: '前向评估失败',
  IMMEDIATE_COMPLETE_SEED_UNAVAILABLE: '缺少紧邻的完整 seed 会话',
  SEED_NOT_KNOWN_AT_TARGET_OPEN: 'seed 数据在目标开盘时尚不可用',
  SESSION_LOCAL_FEATURE_DRIFT: '日内特征发生偏移',
  SOURCE_VERSION_SUPERSEDED: '冻结源版本已被替换',
  TARGET_EVIDENCE_NOT_KNOWN_AT_EVALUATION: '目标证据在评估时尚不可用',
  TARGET_INPUT_HASH_MISMATCH: '目标输入哈希不一致',
  TARGET_SESSION_INCOMPLETE: '目标会话不完整',
  TARGET_STATE_NOT_FLAT: '目标交易日结束时影子状态仍有持仓',
}

function shadowForwardExclusionLabel(reason: string | null): string {
  if (!reason) return '-'
  return shadowForwardExclusionLabels[reason] ?? reason
}

function openShadowForwardDialog() {
  if (!shadowForwardCanRegister.value) return
  shadowForwardOnlyConfirmed.value = false
  shadowForwardNoPromotionConfirmed.value = false
  shadowForwardDialogVisible.value = true
}

function resetShadowForwardConfirmations() {
  shadowForwardOnlyConfirmed.value = false
  shadowForwardNoPromotionConfirmed.value = false
}

async function registerShadowForwardValidation() {
  const symbol = shadowConfig.value?.symbol
  const version = selectedShadowVersion.value
  const generation = shadowRequestGeneration.value
  if (
    !symbol
    || !version
    || !shadowForwardCanRegister.value
    || !shadowForwardOnlyConfirmed.value
    || !shadowForwardNoPromotionConfirmed.value
  ) return
  shadowForwardRegistering.value = true
  try {
    const result = await registerStrategyShadowForwardValidation({
      symbol,
      source_config_version: version,
      candidate_algorithm_version: shadowForwardCandidateVersion,
      confirm_forward_only: true,
      confirm_no_automatic_promotion: true,
    })
    if (
      generation !== shadowRequestGeneration.value
      || selectedShadowSymbol.value !== symbol
      || selectedShadowVersion.value !== version
    ) return
    shadowForwardValidation.value = result
    shadowForwardError.value = ''
    shadowForwardDialogVisible.value = false
    ElMessage.success('前向验证候选已冻结')
  } catch (error: unknown) {
    if (
      generation !== shadowRequestGeneration.value
      || selectedShadowSymbol.value !== symbol
      || selectedShadowVersion.value !== version
    ) return
    ElMessage.error(resolveErrorMessage(error, '冻结前向验证候选失败'))
  } finally {
    if (
      generation === shadowRequestGeneration.value
      && selectedShadowSymbol.value === symbol
      && selectedShadowVersion.value === version
    ) shadowForwardRegistering.value = false
  }
}

async function pollStrategyShadow() {
  void loadOpeningMomentumShadow()
  const symbol = shadowConfig.value?.symbol
  if (!symbol || !selectedShadowVersion.value) return
  const version = selectedShadowVersion.value
  const generation = shadowRequestGeneration.value
  try {
    const [status, evaluation] = await Promise.all([
      getStrategyShadowStatus(symbol),
      getStrategyShadowEvaluation(symbol, version),
    ])
    if (
      generation !== shadowRequestGeneration.value
      || selectedShadowSymbol.value !== symbol
      || selectedShadowVersion.value !== version
    ) return
    shadowStatus.value = status
    shadowStatusFetchedAtMs.value = Date.now()
    shadowEvaluation.value = evaluation
  } catch {
    // Keep the last good snapshot; the next manual refresh exposes the error.
  }
}

function gateShare(count: number): string {
  const total = shadowStatus.value?.metrics.bars ?? 0
  return total > 0 ? `${((count / total) * 100).toFixed(1)}%` : '0.0%'
}

function formatNullable(value: number | null, precision = 2): string {
  return value == null || !Number.isFinite(value) ? '-' : value.toFixed(precision)
}

function formatBps(value: number): string {
  return Number.isFinite(value) ? `${value.toFixed(1)} bps` : '-'
}

function formatNullableBps(value: number | null): string {
  return value == null ? '-' : formatBps(value)
}

function formatPercent(value: number): string {
  return Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '-'
}

function formatNullablePercent(value: number | null): string {
  return value == null ? '-' : formatPercent(value)
}

function shadowMarketTimeZone(): string {
  const symbol = selectedShadowSymbol.value || shadowConfig.value?.symbol || ''
  return symbol.endsWith('.HK') ? 'Asia/Hong_Kong' : 'America/New_York'
}

function formatMarketClock(value: string | null): string {
  if (!value) return '尚未就绪'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: shadowMarketTimeZone(),
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(parsed)
}

function formatMarketDateTime(value: string | null): string {
  if (!value) return '-'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: shadowMarketTimeZone(),
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(parsed)
}

function formatForwardDateTime(value: string | null): string {
  if (!value) return '-'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '-'
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: shadowForwardRegistration.value?.market_timezone || shadowMarketTimeZone(),
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).format(parsed)
  } catch {
    return '-'
  }
}

function formatShadowSessionHour(sessionHour: number): string {
  if (!Number.isInteger(sessionHour) || sessionHour < 0 || sessionHour > 23) return '-'
  const hour = String(sessionHour).padStart(2, '0')
  return `${hour}:00-${hour}:59`
}

function formatEligibilityRate(eligibleBars: number, bars: number): string {
  return bars > 0 ? `${((eligibleBars / bars) * 100).toFixed(1)}%` : '-'
}

function formatSignedCount(value: number): string {
  if (!Number.isFinite(value)) return '-'
  return value > 0 ? `+${value}` : `${value}`
}

function formatSignedNullableCount(value: number | null): string {
  return value == null ? '-' : formatSignedCount(value)
}

function formatSignedNullable(value: number | null, precision = 2): string {
  if (value == null || !Number.isFinite(value)) return '-'
  const formatted = value.toFixed(precision)
  return value > 0 ? `+${formatted}` : formatted
}

function formatNullableBoolean(value: boolean | null): string {
  if (value == null) return '-'
  return value ? '是' : '否'
}

function formatGateCounts(counts: Record<string, number>): string {
  const values = Object.entries(counts)
    .filter(([, count]) => count > 0)
    .sort(([leftGate, leftCount], [rightGate, rightCount]) => (
      rightCount - leftCount || leftGate.localeCompare(rightGate)
    ))
    .slice(0, 3)
    .map(([gate, count]) => `${gate} ${count}`)
  return values.join('；') || '-'
}

function shadowWarmupVariantLabel(label: StrategyShadowWarmupVariant['label']): string {
  return label === 'SESSION_LOCAL' ? '日内冷启动' : '因果趋势预热'
}

function formatExitReasons(reasons: Record<string, number>): string {
  const values = Object.entries(reasons)
    .filter(([, count]) => count > 0)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([reason, count]) => `${reason} ${count}`)
  return values.join('；') || '-'
}

function shortVersion(version: string): string {
  return version ? `配置 ${version.slice(0, 8)}` : '配置未版本化'
}

function shadowPositionLabel(position: string): string {
  return position === 'LONG' ? '虚拟多仓' : '空仓'
}

function shadowActionTagType(action: string): 'success' | 'warning' | 'danger' | 'info' {
  const normalized = action.toUpperCase()
  if (normalized === 'FILL_ENTRY') return 'success'
  if (normalized === 'EXIT_LONG') return 'danger'
  if (normalized === 'ARM_LONG' || normalized === 'SUBMIT_ENTRY' || normalized.startsWith('CANCEL_')) return 'warning'
  return 'info'
}

function exportShadowDecisions() {
  const rows = shadowDecisions.value.map((row) => ({
    observed_at: row.observed_at,
    symbol: row.symbol,
    action: row.action,
    reason: row.reason,
    price: row.price,
    vwap_1m: row.vwap_1m ?? '',
    zscore_1m: row.zscore_1m ?? '',
    vwap_5m: row.vwap_5m ?? '',
    zscore_5m: row.zscore_5m ?? '',
    adx: row.adx ?? '',
    realized_vol: row.realized_vol ?? '',
    regime_eligible: row.regime_eligible ? 'yes' : 'no',
    breach_armed: row.breach_armed ? 'yes' : 'no',
    virtual_position: row.virtual_position,
    net_pnl: row.net_pnl ?? '',
    exit_reason: row.exit_reason ?? '',
    holding_minutes: row.holding_minutes ?? '',
    mae_pct: row.mae_pct ?? '',
    mfe_pct: row.mfe_pct ?? '',
    config_version: row.config_version,
  }))
  downloadCsv(`strategy_v2_shadow_page_${shadowDecisionPage.value}.csv`, [
    { key: 'observed_at', label: 'observed_at' },
    { key: 'symbol', label: 'symbol' },
    { key: 'action', label: 'action' },
    { key: 'reason', label: 'reason' },
    { key: 'price', label: 'price' },
    { key: 'vwap_1m', label: 'vwap_1m' },
    { key: 'zscore_1m', label: 'zscore_1m' },
    { key: 'vwap_5m', label: 'vwap_5m' },
    { key: 'zscore_5m', label: 'zscore_5m' },
    { key: 'adx', label: 'adx' },
    { key: 'realized_vol', label: 'realized_vol' },
    { key: 'regime_eligible', label: 'regime_eligible' },
    { key: 'breach_armed', label: 'breach_armed' },
    { key: 'virtual_position', label: 'virtual_position' },
    { key: 'net_pnl', label: 'net_pnl' },
    { key: 'exit_reason', label: 'exit_reason' },
    { key: 'holding_minutes', label: 'holding_minutes' },
    { key: 'mae_pct', label: 'mae_pct' },
    { key: 'mfe_pct', label: 'mfe_pct' },
    { key: 'config_version', label: 'config_version' },
  ], rows)
  ElMessage.success(`已导出 ${rows.length} 条影子决策`)
}

watch(activeTab, (tab) => {
  if (tab === 'runtime' && !runtimeLoaded.value && !runtimeLoading.value) void loadRuntimeStatus()
  if (tab === 'strategy-shadow' && !shadowLoaded.value && !shadowLoading.value) void loadStrategyShadow()
})

onMounted(async () => {
  await Promise.all([loadVersions(), loadExperimentNames()])
})

const shadowClock = window.setInterval(() => {
  shadowNowMs.value = Date.now()
}, 1000)
const shadowPoll = window.setInterval(() => {
  if (activeTab.value === 'strategy-shadow' && !shadowLoading.value) void pollStrategyShadow()
}, 15_000)

onBeforeUnmount(() => {
  window.clearInterval(shadowClock)
  window.clearInterval(shadowPoll)
})
</script>

<style scoped>
.lab-page {
  padding: 16px;
}

.runtime-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.muted {
  color: #6b7280;
  font-size: 13px;
}

.runtime-grid {
  margin-bottom: 12px;
}

.runtime-card {
  margin-top: 12px;
}

.runtime-alert + .runtime-alert {
  margin-top: 8px;
}

.negative {
  color: #c43838;
}

.runtime-analysis {
  color: #4b5563;
  line-height: 1.5;
}

.runtime-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.usage-actions,
.usage-types {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.usage-actions :deep(.el-select) {
  width: 110px;
}

.usage-metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

.usage-metrics :deep(.metric-stat) {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
  background: #f5f7fa;
}

.usage-metrics :deep(.metric-label),
.usage-metrics :deep(.metric-value) {
  display: block;
}

.usage-types {
  margin-top: 12px;
}

.budget-bar {
  margin-top: 12px;
}

.budget-bar-label {
  margin-bottom: 6px;
  color: #909399;
  font-size: 12px;
}

.budget-bar-note {
  display: block;
  margin-top: 4px;
  color: #909399;
  font-size: 12px;
}

.shadow-toolbar,
.shadow-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.shadow-toolbar {
  margin-bottom: 12px;
}

.shadow-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.shadow-toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.shadow-toolbar-actions :deep(.el-select) {
  width: 190px;
}

.shadow-section {
  width: 100%;
  padding: 16px 0;
  border-bottom: 1px solid #e5e7eb;
}

.shadow-section:last-child {
  border-bottom: 0;
}

.opening-momentum-section {
  padding-top: 0;
  margin-bottom: 16px;
}

.opening-momentum-latest,
.opening-momentum-metrics,
.opening-momentum-variants {
  margin-top: 16px;
}

.opening-momentum-variants {
  overflow-x: auto;
}

.opening-momentum-variants :deep(.el-table) {
  min-width: 900px;
}

.shadow-section-header {
  margin-bottom: 12px;
}

.shadow-section-header h3 {
  margin: 0;
  font-size: 16px;
}

.shadow-section-header small {
  display: block;
  margin-top: 4px;
  color: #6b7280;
}

.shadow-form {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0 16px;
}

.shadow-form :deep(.el-input-number) {
  width: 100%;
}

.shadow-facts,
.shadow-metrics-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.shadow-facts > div {
  min-width: 0;
  padding: 10px 12px;
  background: #f5f7fa;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
}

.shadow-facts span,
.shadow-facts strong {
  display: block;
}

.shadow-facts span {
  margin-bottom: 5px;
  color: #6b7280;
  font-size: 12px;
}

.shadow-facts strong {
  overflow-wrap: anywhere;
}

.shadow-metrics-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.shadow-progress-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.shadow-progress-grid span {
  display: block;
  margin-bottom: 6px;
  color: #4b5563;
  font-size: 13px;
}

.shadow-evidence-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  margin: -4px 0 12px;
  color: #6b7280;
  font-size: 13px;
}

.shadow-forward-facts {
  margin-bottom: 16px;
}

.shadow-forward-summary,
.shadow-forward-pair-audit {
  margin-top: 0;
}

.shadow-forward-dialog-summary {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr);
  gap: 8px 12px;
  margin-bottom: 14px;
}

.shadow-forward-dialog-summary span {
  color: #6b7280;
}

.shadow-forward-dialog-summary strong {
  min-width: 0;
  overflow-wrap: anywhere;
}

.shadow-forward-confirmations {
  display: grid;
  gap: 8px;
}

.shadow-forward-confirmations :deep(.el-checkbox) {
  align-items: flex-start;
  height: auto;
  margin-right: 0;
  white-space: normal;
}

.shadow-forward-confirmations :deep(.el-checkbox__label) {
  line-height: 1.45;
  white-space: normal;
}

:global(.shadow-forward-dialog) {
  max-width: calc(100vw - 32px);
}

.shadow-evidence-alert {
  margin-bottom: 10px;
}

.shadow-subsection-title {
  margin: 18px 0 10px;
  font-size: 14px;
  font-weight: 600;
}

.shadow-metrics-grid :deep(.el-statistic) {
  min-width: 0;
  padding: 10px 12px;
  border-left: 3px solid #409eff;
}

.opening-momentum-stat {
  min-width: 0;
  padding: 10px 12px;
  border-left: 3px solid #409eff;
}

.opening-momentum-stat span,
.opening-momentum-stat strong {
  display: block;
}

.opening-momentum-stat span {
  margin-bottom: 10px;
  color: #606266;
  font-size: 13px;
}

.opening-momentum-stat strong {
  font-size: 20px;
  font-weight: 400;
}

.shadow-excursions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  margin-top: 12px;
  color: #4b5563;
  font-size: 13px;
}

.shadow-reason {
  margin: 12px 0 0;
  padding-left: 10px;
  border-left: 3px solid #909399;
  color: #4b5563;
}

.shadow-pagination {
  justify-content: flex-end;
  margin-top: 12px;
}

@media (max-width: 900px) {
  .shadow-form,
  .shadow-metrics-grid,
  .shadow-progress-grid,
  .usage-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 600px) {
  .shadow-toolbar,
  .shadow-section-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .shadow-toolbar-actions {
    width: 100%;
  }

  .shadow-toolbar-actions :deep(.el-select) {
    flex: 1;
    width: auto;
  }

  .shadow-form,
  .shadow-facts,
  .shadow-metrics-grid,
  .shadow-progress-grid,
  .usage-metrics {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
