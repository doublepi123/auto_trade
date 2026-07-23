<template>
  <div class="dashboard-page">
    <el-alert v-if="loadError" type="error" title="无法连接服务器，请检查网络和 API 密钥" show-icon :closable="false" class="dashboard-alert">
      <el-button size="small" type="primary" plain @click="handleRetry">重试连接</el-button>
    </el-alert>

    <el-alert v-if="accountError" type="warning" title="账户资产暂时不可用，请检查券商凭证或稍后重试" show-icon class="dashboard-alert" />

    <section class="dashboard-cockpit" data-testid="dashboard-cockpit">
      <div class="page-heading cockpit-heading">
        <div>
          <h3>交易驾驶舱</h3>
          <p>{{ strategy.symbol || '未配置标的' }} · {{ marketLabel(strategy.market) }} · {{ strategy.short_selling ? '允许做空' : '仅做多' }}</p>
        </div>
        <div class="heading-tags">
          <el-tag :type="realtimeStatusType" effect="plain">{{ realtimeStatusLabel }}</el-tag>
          <el-tag :type="status.runner_running ? 'success' : 'info'" effect="plain">{{ status.runner_running ? '运行器运行中' : '运行器未启动' }}</el-tag>
          <span class="heading-updated" data-testid="cockpit-updated">更新 {{ statusUpdatedAtLabel }}</span>
        </div>
      </div>

      <div
        v-if="recentNotifications.length > 0"
        class="notif-ticker"
        data-testid="dashboard-notif-ticker"
        v-loading="recentNotificationsLoading"
      >
        <span class="ticker-label">通知</span>
        <div class="ticker-items">
          <div
            v-for="n in recentNotifications.slice(0, 3)"
            :key="n.id"
            class="ticker-item"
            role="button"
            tabindex="0"
            @click="router.push('/notifications')"
            @keydown.enter.prevent="router.push('/notifications')"
            @keydown.space.prevent="router.push('/notifications')"
          >
            <el-tag size="small" :type="severityType(n.severity)">{{ n.severity }}</el-tag>
            <span class="ticker-title">{{ n.title }}</span>
            <small>{{ formatTime(n.created_at) }}</small>
          </div>
        </div>
        <el-button link size="small" @click="router.push('/notifications')">查看全部</el-button>
      </div>

      <el-alert
        v-if="statusStale"
        class="cockpit-stale-alert"
        data-testid="dashboard-stale-alert"
        type="warning"
        :closable="false"
        show-icon
        @click="reconnectNow()"
      >
        <template #title>
          行情数据已 {{ staleAgeLabel }} 未更新，显示可能延迟
          <el-button
            link
            type="primary"
            size="small"
            data-testid="stale-reconnect"
            @click.stop="reconnectNow()"
            >立即重连</el-button
          >
        </template>
      </el-alert>

      <div class="status-strip" data-testid="status-strip" v-loading="statusLoading || strategyLoading">
        <div class="strip-item">
          <span>标的</span>
          <strong>{{ strategy.symbol || '未配置' }}</strong>
          <small>{{ marketLabel(strategy.market) }}</small>
        </div>
        <div class="strip-item">
          <span>交易状态</span>
          <strong>{{ status.kill_switch ? '紧急停止' : status.protective_exit_permitted ? '仅减仓' : status.paused ? '已暂停' : status.execution_state === 'REDUCING' ? '减仓中' : '运行中' }}</strong>
          <small :title="status.reduction_reason">{{ status.kill_switch ? '紧急停止开启' : status.protective_exit_permitted ? '保护性退出已授权' : status.paused ? pausedStatusDetail : status.execution_state === 'REDUCING' ? '保护性退出处理中' : '紧急停止关闭' }}</small>
        </div>
        <div class="strip-item">
          <span>引擎状态</span>
          <strong>{{ engineStateLabel(status.engine_state) }}</strong>
          <small>连续亏损 {{ status.consecutive_losses }}</small>
        </div>
        <div class="strip-item">
          <span>LLM</span>
          <strong>{{ llmStatus?.enabled ? `${llmStatus.interval_minutes}m ${llmStatus.shadow_mode ? '影子' : '自动'}` : '未启用' }}</strong>
          <small>{{ llmStatus?.last_analysis_at ? formatTime(llmStatus.last_analysis_at) : '暂无刷新' }}</small>
        </div>
        <div class="strip-item">
          <span>今日盈亏</span>
          <strong :class="metricClass(status.daily_pnl)">{{ signedCurrency(status.daily_pnl) }}</strong>
          <small>{{ pnlLabel(status.daily_pnl) }}</small>
        </div>
        <div class="strip-item" data-testid="session-hours-indicator">
          <span>下单时段</span>
          <strong>{{ status.trading_session_mode === 'RTH_ONLY' ? '仅 RTH' : '不限' }}</strong>
          <small class="session-strip-hint">
            <span class="session-dot" :class="sessionOrderDotClass" />
            {{ sessionOrderHint }}
          </small>
        </div>
      </div>

      <div class="cockpit-grid">
        <section class="cockpit-panel price-panel" data-testid="price-panel" v-loading="statusLoading || strategyLoading">
          <div class="panel-heading">
            <span>最新价格</span>
            <el-tag v-if="statusStale" type="warning" size="small" data-testid="price-stale-tag">{{ staleAgeLabel }}</el-tag>
            <el-tag :type="stateTagType">{{ engineStateLabel(status.engine_state) }}</el-tag>
          </div>
          <div class="price-value">${{ formatNumber(status.last_price) }}</div>
          <div class="range-line" aria-hidden="true">
            <span class="range-fill" :style="{ width: priceRangeWidth }" />
            <span class="range-marker" :style="{ left: priceRangeLeft }" />
          </div>
          <div class="range-labels">
            <span>买入线 ${{ formatNumber(strategy.buy_low) }}</span>
            <span>卖出线 ${{ formatNumber(strategy.sell_high) }}</span>
          </div>
          <div class="mini-grid">
            <div>
              <span>上次触发</span>
              <strong>{{ status.last_trigger_price > 0 ? `$${formatNumber(status.last_trigger_price)}` : '-' }}</strong>
            </div>
            <div>
              <span>最低盈利</span>
              <strong>${{ formatNumber(strategy.min_profit_amount) }}</strong>
            </div>
            <div v-if="status.last_action_message" class="action-message">
              <span>最近动作</span>
              <strong>{{ status.last_action_message }}</strong>
            </div>
          </div>
        </section>

        <section class="cockpit-panel position-panel" data-testid="position-panel" v-loading="accountLoading">
          <div class="panel-heading">
            <span>持仓明细</span>
            <el-tag :type="primaryPosition ? 'success' : 'info'" effect="plain">{{ primaryPosition ? positionSideLabel(primaryPosition.side) : '空仓' }}</el-tag>
          </div>
          <template v-if="primaryPosition">
            <div class="position-symbol">{{ primaryPosition.symbol }}</div>
            <div class="position-main">
              <div>
                <span>数量</span>
                <strong>{{ primaryPosition.quantity.toFixed(0) }}</strong>
              </div>
              <div>
                <span>均价</span>
                <strong>${{ formatNumber(primaryPosition.avg_price) }}</strong>
              </div>
              <div>
                <span>市值</span>
                <strong>${{ formatNumber(primaryPosition.market_value) }}</strong>
              </div>
            </div>
            <div class="pnl-box" :class="metricClass(unrealizedPnl)">
              <span>浮动盈亏</span>
              <strong>{{ signedCurrency(unrealizedPnl) }} / {{ signedPercent(unrealizedPnlPct) }}</strong>
            </div>
          </template>
          <p v-else class="empty-note">暂无持仓</p>
        </section>

        <section class="cockpit-panel metrics-panel" data-testid="metrics-panel" v-loading="metricsLoading">
          <div class="panel-heading">
            <span>近 {{ metricsWindowDays }} 日交易指标</span>
            <el-button
              link
              size="small"
              @click="loadMetrics"
              data-testid="metrics-refresh"
            >刷新</el-button>
          </div>
          <div class="metrics-grid">
            <div class="metric-cell">
              <span class="metric-label">交易笔数</span>
              <strong data-testid="metric-trade-count">{{ metrics.trade_count }}</strong>
            </div>
            <div class="metric-cell">
              <span class="metric-label">胜率</span>
              <strong
                :class="metrics.win_rate >= 50 ? 'metric-positive' : 'metric-negative'"
                data-testid="metric-win-rate"
              >{{ metrics.win_rate.toFixed(1) }}%</strong>
            </div>
            <div class="metric-cell">
              <span class="metric-label">盈亏比</span>
              <strong
                :class="(metrics.profit_factor ?? 0) >= 1 ? 'metric-positive' : 'metric-negative'"
                data-testid="metric-profit-factor"
              >{{ metrics.profit_factor === null ? '—' : metrics.profit_factor.toFixed(2) }}</strong>
            </div>
            <div class="metric-cell">
              <span class="metric-label">Sharpe</span>
              <strong
                :class="(metrics.sharpe_ratio ?? 0) >= 0 ? 'metric-positive' : 'metric-negative'"
                data-testid="metric-sharpe"
              >{{ metrics.sharpe_ratio === null ? '—' : metrics.sharpe_ratio.toFixed(2) }}</strong>
            </div>
            <div class="metric-cell">
              <span class="metric-label">最大回撤</span>
              <strong
                :class="metrics.max_drawdown > 20 ? 'metric-negative' : ''"
                data-testid="metric-max-drawdown"
              >{{ metrics.max_drawdown.toFixed(1) }}%</strong>
            </div>
            <div class="metric-cell">
              <span class="metric-label">均 PnL</span>
              <strong
                :class="metrics.avg_pnl >= 0 ? 'metric-positive' : 'metric-negative'"
                data-testid="metric-avg-pnl"
              >${{ formatNumber(metrics.avg_pnl) }}</strong>
            </div>
          </div>
          <p v-if="metrics.trade_count === 0 && !metricsLoading" class="empty-note">尚无成交记录</p>
        </section>

        <section class="cockpit-panel llm-panel" data-testid="llm-panel" v-loading="llmStatusLoading">
          <div class="panel-heading">
            <span>LLM 智能区间</span>
            <div style="display: flex; align-items: center; gap: 8px">
              <el-tag v-if="llmStatus" data-testid="llm-policy-mode" :type="llmStatus.shadow_mode ? 'warning' : 'success'" effect="plain">
                {{ llmStatus.shadow_mode ? '影子观察' : '实盘应用' }}
              </el-tag>
              <el-tag :type="llmStatus?.enabled ? 'success' : 'info'" effect="plain">{{ llmStatus?.enabled ? '已启用' : '已禁用' }}</el-tag>
            </div>
          </div>
          <template v-if="llmStatus">
            <template v-if="llmStatus.current_suggestion">
              <div class="llm-decision">
                <strong>{{ llmStatus.current_suggestion.confidence_score.toFixed(2) }}</strong>
                <span>置信度</span>
              </div>
              <p class="llm-range">建议区间 {{ llmStatus.current_suggestion.buy_low.toFixed(2) }} ~ {{ llmStatus.current_suggestion.sell_high.toFixed(2) }}</p>
              <p class="llm-analysis">{{ llmStatus.current_suggestion.analysis }}</p>
              <div class="llm-meta">
                <span>最近刷新：{{ llmStatus.last_analysis_at ? formatDateTime(llmStatus.last_analysis_at) : '-' }}</span>
                <span>下次分析：{{ llmStatus.next_analysis_at ? formatDateTime(llmStatus.next_analysis_at) : '-' }}</span>
              </div>
              <div class="llm-apply-state">
                <el-tag v-if="llmStatus.applied_values" type="success" effect="plain">已应用</el-tag>
                <span v-if="llmStatus.applied_values">当前 {{ llmStatus.applied_values.buy_low.toFixed(2) }} ~ {{ llmStatus.applied_values.sell_high.toFixed(2) }}</span>
                <span v-else-if="llmStatus.shadow_mode && llmStatus.last_applied_values">历史实盘应用 {{ llmStatus.last_applied_values.buy_low.toFixed(2) }} ~ {{ llmStatus.last_applied_values.sell_high.toFixed(2) }}</span>
                <span v-else-if="llmStatus.reject_reason">未应用：{{ llmStatus.reject_reason }}</span>
              </div>
            </template>
            <p v-else class="empty-note">暂无 LLM 建议</p>

            <div class="llm-schedule" data-testid="llm-schedule">
              <div class="llm-budget-bar" data-testid="llm-budget-bar">
                <div class="budget-cell" data-testid="llm-budget-tracked">
                  <span>本周期标的</span>
                  <strong>{{ (llmStatus.budget?.tracked_symbol_count ?? 0) }}/{{ (llmStatus.budget?.max_symbols_per_cycle ?? 0) }}</strong>
                </div>
                <div class="budget-cell" data-testid="llm-budget-hourly">
                  <span>小时分析</span>
                  <strong>{{ (llmStatus.budget?.used_analyses_last_hour ?? 0) }}/{{ (llmStatus.budget?.max_analyses_per_hour ?? 0) }}</strong>
                </div>
                <div class="budget-cell" data-testid="llm-budget-remaining">
                  <span>剩余次数</span>
                  <strong>{{ llmStatus.budget?.remaining_analyses_this_hour ?? 0 }}</strong>
                </div>
              </div>

              <div class="llm-symbol-status" data-testid="llm-symbol-status">
                <h5 class="subsection-title">标的调度状态</h5>
                <el-table
                  :data="llmStatus.symbol_statuses || []"
                  size="small"
                  class="responsive-table"
                  data-testid="llm-symbol-status-table"
                  empty-text="暂无标的调度状态"
                >
                  <el-table-column prop="symbol" label="标的" min-width="110" />
                  <el-table-column label="角色" min-width="80">
                    <template #default="{ row }">
                      <el-tag :type="row.is_primary ? 'success' : 'info'" size="small">{{ row.is_primary ? '主标的' : '观察' }}</el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="挂单" min-width="70">
                    <template #default="{ row }">{{ row.has_pending_order ? '有' : '无' }}</template>
                  </el-table-column>
                  <el-table-column label="下次分析" min-width="130">
                    <template #default="{ row }">{{ row.next_analysis_at ? formatDateTime(row.next_analysis_at) : '-' }}</template>
                  </el-table-column>
                  <el-table-column label="买入冷却" min-width="90">
                    <template #default="{ row }">{{ formatCooldown(row.buy_cooldown_remaining_seconds) }}</template>
                  </el-table-column>
                  <el-table-column label="卖出冷却" min-width="90">
                    <template #default="{ row }">{{ formatCooldown(row.sell_cooldown_remaining_seconds) }}</template>
                  </el-table-column>
                  <el-table-column label="最近状态" min-width="120" show-overflow-tooltip>
                    <template #default="{ row }">
                      <span>{{ row.last_status || '-' }}</span>
                      <small v-if="row.last_skip_reason" class="llm-skip-reason">（{{ row.last_skip_reason }}）</small>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </template>
          <p v-else class="empty-note">暂无 LLM 状态</p>
        </section>

        <section
          class="cockpit-panel action-panel"
          data-testid="quick-actions"
          role="region"
          aria-labelledby="quick-actions-heading"
          v-loading="statusLoading"
        >
          <div class="panel-heading">
            <div>
              <span id="quick-actions-heading">操作控制</span>
              <p class="panel-caption">全局控制，作用于全部标的运行时</p>
            </div>
            <el-tag :type="status.kill_switch ? 'danger' : status.execution_state === 'REDUCING' || status.paused ? 'warning' : 'success'" effect="plain">
              {{ status.kill_switch ? '紧急停止' : status.protective_exit_permitted ? '仅减仓' : status.paused ? '暂停中' : status.execution_state === 'REDUCING' ? '减仓中' : '可交易' }}
            </el-tag>
          </div>
          <div class="action-grid">
            <el-button
              type="primary"
              @click="handleStart"
              :disabled="status.kill_switch || status.runner_running"
              data-testid="dashboard-start-btn"
              :aria-label="`启动交易${status.kill_switch ? '（当前紧急停止已启用）' : ''}`"
            >启动</el-button>
            <el-button
              type="success"
              @click="handleResume"
              :disabled="!status.paused || status.kill_switch"
              data-testid="dashboard-resume-btn"
              aria-label="恢复交易"
            >恢复</el-button>
            <el-button
              type="warning"
              @click="handlePause"
              :disabled="status.paused || status.kill_switch"
              data-testid="dashboard-pause-btn"
              aria-label="暂停交易"
            >暂停</el-button>
            <el-button
              v-if="protectiveExitControlVisible"
              class="protective-exit-button"
              :type="status.protective_exit_permitted ? 'info' : 'danger'"
              plain
              :loading="controlInFlight"
              @click="handleProtectiveExits"
              data-testid="dashboard-protective-exit-btn"
              :aria-pressed="status.protective_exit_permitted"
              :aria-label="status.protective_exit_permitted ? '撤销仅减仓许可' : '启用仅减仓许可'"
            >{{ status.protective_exit_permitted ? '撤销仅减仓许可' : '仅允许减仓' }}</el-button>
            <el-button
              type="danger"
              @click="handleStop"
              data-testid="dashboard-stop-btn"
              aria-label="停止交易"
            >停止</el-button>
            <el-button
              class="kill-button"
              type="danger"
              plain
              @click="handleKillSwitch"
              data-testid="dashboard-kill-btn"
              :aria-pressed="status.kill_switch"
              aria-label="紧急停止：立即暂停所有交易并要求人工解除"
            >紧急停止</el-button>
            <el-button
              v-if="status.kill_switch"
              type="success"
              plain
              @click="handleDisableKillSwitch"
              data-testid="dashboard-disable-kill-btn"
              aria-label="解除紧急停止"
            >解除紧急停止</el-button>
          </div>
        </section>
      </div>
    </section>

    <section class="detail-panel multi-symbol-panel" data-testid="multi-symbol-snapshots" v-loading="multiSymbolLoading">
      <div class="section-title">
        <h4>多标的观察</h4>
        <el-button size="small" plain @click="refreshMultiSymbols">刷新</el-button>
      </div>

      <el-alert
        v-if="multiSymbolError"
        type="warning"
        :closable="false"
        :title="multiSymbolError"
        class="multi-symbol-alert"
      />

      <el-empty v-if="!multiSymbolLoading && multiSymbolSnapshots.length === 0" description="暂无观察标的" />
      <el-table v-else :data="multiSymbolSnapshots" size="small" class="responsive-table">
        <el-table-column prop="symbol" label="标的" min-width="120" />
        <el-table-column prop="alias" label="别名" min-width="120" />
        <el-table-column prop="market" label="市场" width="80" />
        <el-table-column label="最新价" width="110">
          <template #default="{ row }">{{ formatNumber(row.last_price) }}</template>
        </el-table-column>
        <el-table-column label="买一" width="110">
          <template #default="{ row }">{{ formatNumber(row.bid) }}</template>
        </el-table-column>
        <el-table-column label="卖一" width="110">
          <template #default="{ row }">{{ formatNumber(row.ask) }}</template>
        </el-table-column>
        <el-table-column label="更新时间" width="120">
          <template #default="{ row }">{{ formatTime(row.timestamp) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.is_trading_target" type="success" size="small">当前交易</el-tag>
            <span v-else>观察</span>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section class="detail-panel diagnostics-panel" data-testid="dashboard-diagnostics" v-loading="diagnosticsLoading">
      <div class="section-title">
        <h4>运行诊断</h4>
        <div>
          <el-button size="small" plain :disabled="!diagnostics" data-testid="dash-diagnostics-export" @click="exportDiagnostics">导出 CSV</el-button>
          <el-button size="small" plain @click="loadDiagnostics">刷新</el-button>
        </div>
      </div>
      <template v-if="diagnostics">
        <div class="diagnostics-grid">
          <div class="diagnostic-block">
            <span>线程存活</span>
            <strong>{{ diagnostics.thread_alive ? '是' : '否' }}</strong>
          </div>
          <div class="diagnostic-block">
            <span>行情流</span>
            <strong>{{ diagnostics.quotes_subscribed ? '已订阅' : '未订阅' }}</strong>
            <small>最近推送 {{ formatAgeSeconds(diagnostics.quote_stream.last_push_age_seconds) }}</small>
          </div>
          <div class="diagnostic-block">
            <span>最近报价</span>
            <strong>{{ formatAgeSeconds(diagnostics.quote_stream.last_quote_age_seconds) }}</strong>
            <small>{{ diagnostics.quote_stream.recent_quote_count }} 条样本</small>
          </div>
          <div class="diagnostic-block">
            <span>挂单标的</span>
            <strong>{{ diagnostics.pending_order_symbols.length > 0 ? diagnostics.pending_order_symbols.join(', ') : '无' }}</strong>
            <small>运行时总数 {{ diagnostics.symbol_runtimes.length }} 个</small>
            <small>{{ diagnostics.trigger_in_flight ? '触发处理中' : '空闲' }}</small>
          </div>
        </div>

        <div class="live-safety" data-testid="dashboard-live-safety">
          <h5>实时安全参数</h5>
          <el-descriptions :column="isMobile ? 1 : 3" border size="small">
            <el-descriptions-item v-if="diagnostics.live_safety.full_buying_power_usage_enabled" label="入场资金">
              <el-tag
                type="danger"
                size="small"
                data-testid="full-buying-power-mode"
              >
                全部购买力
              </el-tag>
              <span class="safety-inline-note">
                {{ formatNumber(diagnostics.live_safety.buying_power_usage_pct) }}%
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="做空开仓">
              <el-tag :type="diagnostics.live_safety.short_entries_enabled ? 'danger' : 'success'" size="small">
                {{ diagnostics.live_safety.short_entries_enabled ? '允许' : '关闭' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="持仓加码">
              <el-tag :type="diagnostics.live_safety.allow_position_addons ? 'warning' : 'success'" size="small">
                {{ diagnostics.live_safety.allow_position_addons ? '允许' : '关闭' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="LLM 模式">
              <el-tag :type="diagnostics.live_safety.llm_shadow_mode ? 'info' : 'warning'" size="small">
                {{ diagnostics.live_safety.llm_shadow_mode ? '影子' : '实时' }}
              </el-tag>
              <span class="safety-inline-note">
                {{ diagnostics.live_safety.llm_order_execution_enabled ? '可下单' : '禁下单' }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="最大持仓数量">
              <span data-testid="max-position-quantity-value">
                {{
                  diagnostics.live_safety.full_buying_power_usage_enabled
                    ? '由券商购买力决定'
                    : formatNumber(diagnostics.live_safety.max_position_quantity)
                }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="最大持仓市值">
              <span data-testid="max-position-notional-value">
                {{
                  diagnostics.live_safety.full_buying_power_usage_enabled
                    ? '由券商购买力决定'
                    : `$${formatNumber(diagnostics.live_safety.max_position_notional)}`
                }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="单笔最大风险">
              <span data-testid="max-risk-per-trade-value">
                {{
                  diagnostics.live_safety.full_buying_power_usage_enabled
                    ? '由券商购买力决定'
                    : `$${formatNumber(diagnostics.live_safety.max_risk_per_trade)}`
                }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="止损阈值">
              {{ formatNumber(diagnostics.live_safety.stop_loss_pct) }}%
            </el-descriptions-item>
            <el-descriptions-item label="最长持仓">
              {{ diagnostics.live_safety.max_holding_minutes }} 分钟
            </el-descriptions-item>
            <el-descriptions-item label="停止开仓">
              收盘前 {{ diagnostics.live_safety.entry_cutoff_minutes_before_close }} 分钟
            </el-descriptions-item>
            <el-descriptions-item label="强制平仓">
              收盘前 {{ diagnostics.live_safety.flatten_minutes_before_close }} 分钟
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <el-table :data="diagnostics.symbol_runtimes" size="small" class="responsive-table">
          <el-table-column prop="symbol" label="标的" min-width="120" />
          <el-table-column label="限额" min-width="130">
            <template #default="{ row }">
              <el-tag v-if="row.position_limit_breaches.length" type="danger" size="small">
                超限 {{ row.position_limit_breaches.length }} 项
              </el-tag>
              <el-tag v-else type="success" size="small">正常</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="持仓暴露" min-width="140">
            <template #default="{ row }">
              <span v-if="row.position_quantity > 0">
                {{ formatNumber(row.position_quantity) }} 股 / ${{ formatNumber(row.position_notional) }}
              </span>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" min-width="100">
            <template #default="{ row }">{{ engineStateLabel(row.engine_state) }}</template>
          </el-table-column>
          <el-table-column label="角色" min-width="90">
            <template #default="{ row }">
              <el-tag :type="row.is_primary ? 'success' : 'info'" size="small">{{ row.is_primary ? '主标的' : '观察标的' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="最新价" min-width="90">
            <template #default="{ row }">${{ formatNumber(row.last_price) }}</template>
          </el-table-column>
          <el-table-column label="最近触发" min-width="90">
            <template #default="{ row }">{{ row.last_trigger_price > 0 ? `$${formatNumber(row.last_trigger_price)}` : '-' }}</template>
          </el-table-column>
          <el-table-column label="样本数" min-width="70">
            <template #default="{ row }">{{ row.recent_quote_count }}</template>
          </el-table-column>
          <el-table-column label="挂单" min-width="80">
            <template #default="{ row }">{{ row.has_pending_order ? '存在挂单' : '无' }}</template>
          </el-table-column>
        </el-table>
      </template>
      <p v-else class="empty-note">暂无诊断数据</p>
    </section>

    <section class="detail-panel" data-testid="position-pnl-section">
      <PositionPnlPanel />
      <RiskHistoryPanel />
      <EquityCurvePanel />
      <SymbolAttributionPanel />
      <SessionClockPanel :symbol="strategy.symbol" />
    </section>

    <section class="chart-grid" data-testid="dashboard-charts">
      <div class="chart-controls">
        <div class="chart-symbol-controls">
          <strong data-testid="chart-symbol-current">{{ selectedChartSymbol || strategy.symbol || '未配置标的' }}</strong>
          <el-select
            v-model="selectedChartSymbol"
            size="small"
            placeholder="选择图表标的"
            style="width: 180px"
            data-testid="chart-symbol-select"
          >
            <el-option
              v-for="option in chartSymbolOptions"
              :key="option.symbol"
              :label="option.label"
              :value="option.symbol"
            />
          </el-select>
          <el-button
            size="small"
            text
            :disabled="!selectedChartSymbol"
            :data-testid="symbolIsPinned(selectedChartSymbol) ? 'chart-symbol-pinned' : 'chart-symbol-pin'"
            @click="pinSymbol(selectedChartSymbol)"
            >{{ symbolIsPinned(selectedChartSymbol) ? '已固定' : '📌 固定' }}</el-button
          >
        </div>
        <div v-if="pinnedSymbols.length" class="pinned-symbols" data-testid="pinned-symbols-bar">
          <span class="pinned-label">固定：</span>
          <el-tag
            v-for="sym in pinnedSymbols"
            :key="sym"
            :type="sym === selectedChartSymbol ? 'primary' : 'info'"
            size="small"
            class="pinned-chip"
            closable
            data-testid="pinned-chip"
            @close="unpinSymbol(sym)"
            @click="requestSymbol(sym)"
          >
            {{ sym }}
          </el-tag>
        </div>
        <el-button v-if="isMobile" size="small" text @click="chartExpanded = !chartExpanded"
          >{{ chartExpanded ? '收起图表' : '展开图表' }}</el-button
        >
      </div>
      <div v-show="chartExpanded || !isMobile" class="chart-panels">
        <PriceChart
          :points="chartPoints"
          :markers="tradeMarkers"
          :buy-low="selectedChartIsPrimary ? strategy.buy_low : 0"
          :sell-high="selectedChartIsPrimary ? strategy.sell_high : 0"
        />
        <PnLChart :points="chartPoints" />
      </div>
    </section>

    <section class="detail-grid">
      <div class="detail-panel account-panel" v-loading="accountLoading">
        <div class="section-title">
          <h4>总资产</h4>
          <strong :class="account.available ? 'metric-positive' : 'metric-negative'">${{ formatNumber(account.total_assets) }}</strong>
          <el-tag v-if="accountRefreshing && !accountLoading" size="small" type="info">刷新中</el-tag>
        </div>
        <h4 class="subsection-title">现金余额</h4>
        <el-table :data="account.cash_balances" size="small" v-if="account.cash_balances.length > 0" class="responsive-table">
          <el-table-column prop="currency" label="币种" min-width="80" />
          <el-table-column prop="available_cash" label="可用" min-width="120">
            <template #default="{ row }">${{ formatNumber(row.available_cash) }}</template>
          </el-table-column>
          <el-table-column prop="frozen_cash" label="冻结" min-width="120">
            <template #default="{ row }">${{ formatNumber(row.frozen_cash) }}</template>
          </el-table-column>
        </el-table>
        <p v-else-if="!account.available" class="empty-note">数据不可用</p>
        <p v-else class="empty-note">暂无数据</p>
      </div>

      <div class="detail-panel" v-loading="strategyLoading || statusLoading">
        <div class="section-title">
          <h4>行情信息</h4>
          <span>{{ strategy.symbol || '未配置' }}</span>
        </div>
        <div class="strategy-list">
          <div><span>买入价下限</span><strong>${{ formatNumber(strategy.buy_low) }}</strong></div>
          <div><span>卖出价上限</span><strong>${{ formatNumber(strategy.sell_high) }}</strong></div>
          <div><span>做空</span><strong>{{ strategy.short_selling ? '是' : '否' }}</strong></div>
          <div><span>暂停自动恢复</span><strong>{{ strategy.auto_resume_minutes }} 分钟</strong></div>
        </div>
        <h4 class="subsection-title">风控状态</h4>
        <div class="risk-list">
          <span>全局紧急停止：{{ status.kill_switch ? '已开启' : '已关闭' }}</span>
          <span>全局暂停状态：{{ status.paused ? '已暂停' : '运行中' }}</span>
          <span>单日最大亏损：${{ formatNumber(strategy.max_daily_loss) }}</span>
          <span data-testid="drawdown-risk-status">高水位回撤：${{ formatNumber(status.drawdown_amount) }} / {{ drawdownLimit === null ? '未设置' : `$${formatNumber(drawdownLimit)}` }}（峰值 ${{ formatNumber(status.peak_realized_pnl) }}）</span>
        </div>
      </div>

      <div class="detail-panel recent-orders" data-testid="recent-orders" v-loading="recentOrdersLoading">
        <div class="section-title">
          <h4>最近订单</h4>
          <span>{{ recentOrders.length }} 条</span>
        </div>
        <el-table :data="recentOrders" size="small" v-if="recentOrders.length > 0" class="responsive-table">
          <el-table-column prop="side" label="方向" min-width="90" />
          <el-table-column prop="quantity" label="数量" min-width="70">
            <template #default="{ row }">{{ row.quantity.toFixed(0) }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" min-width="100" />
          <el-table-column prop="executed_price" label="成交价" min-width="100">
            <template #default="{ row }">{{ row.executed_price ? `$${formatNumber(row.executed_price)}` : '-' }}</template>
          </el-table-column>
        </el-table>
        <p v-else class="empty-note">暂无订单</p>
      </div>

      <div class="detail-panel recent-events" data-testid="recent-events" v-loading="recentEventsLoading">
        <div class="section-title">
          <h4>决策时间线</h4>
          <span>{{ recentEvents.length }} 条</span>
        </div>
        <div v-if="recentEvents.length > 0" class="event-list">
          <div v-for="event in recentEvents" :key="`${event.source}-${event.id}`" class="event-row">
            <div class="event-main">
              <el-tag size="small" :type="eventTagType(event.event_type, event.status, event.source)" effect="plain">
                {{ event.source === 'audit' ? auditActionLabel(event.event_type) : tradeEventTypeLabel(event.event_type) }}
              </el-tag>
              <strong>{{ event.symbol || event.broker_order_id || '-' }}</strong>
              <small>{{ formatDateTime(event.created_at) }}</small>
            </div>
            <p>{{ event.message || event.status || '-' }}</p>
            <small
              v-if="event.event_type === EVENT_TYPE.ORDER_SKIPPED && event.payload?.skip_category"
              class="skip-category"
            >{{ skipCategoryLabel(String(event.payload.skip_category)) }}</small>
          </div>
        </div>
        <p v-else class="empty-note">暂无决策事件</p>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import PriceChart from '../components/PriceChart.vue'
import PnLChart from '../components/PnLChart.vue'
import PositionPnlPanel from '../components/PositionPnlPanel.vue'
import RiskHistoryPanel from '../components/RiskHistoryPanel.vue'
import EquityCurvePanel from '../components/EquityCurvePanel.vue'
import SymbolAttributionPanel from '../components/SymbolAttributionPanel.vue'
import SessionClockPanel from '../components/SessionClockPanel.vue'
import { useDashboardData } from '../composables/useDashboardData'
import { useConnectionHealth } from '../composables/useConnectionHealth'
import { useAccountRefresh } from '../composables/useAccountRefresh'
import { useMultiSymbolSnapshots } from '../composables/useMultiSymbolSnapshots'
import { useStatusHistorySeries } from '../composables/useStatusHistorySeries'
import { useSymbolStore } from '../composables/useSymbolStore'
import { usePinnedSymbols } from '../composables/usePinnedSymbols'
import { useRegisterViewRefresh } from '../composables/useViewRefreshRegistry'
import { useDiagnosticsSnapshot } from '../composables/useDiagnosticsSnapshot'
import { startTrading, stopTrading, pauseTrading, resumeTrading, enableProtectiveExits, disableProtectiveExits, activateKillSwitch, disableKillSwitch, getLLMIntervalStatus, getNotifications, getOrders, getTradeEvents, getMetricsSummary } from '../api'
import type { LLMIntervalStatus, NotificationLogOut, OrderRecord, Position, StatusHistoryPoint, TradeEventRecord } from '../types'
import { engineStateLabel, auditActionLabel, marketLabel, positionSideLabel, skipCategoryLabel, tradeEventTypeLabel } from '../utils/labels'
import { EVENT_TYPE } from '../utils/constants'
import { downloadCsv } from '../utils/csv'
import { relativeAgeLabel } from '../utils/time'
import { formatNumber, signedCurrency, signedPercent } from '../utils/format'
import { resolveErrorMessage } from '../utils/error'

type CypressWindow = Window & { Cypress?: unknown }
const multiSymbols = useMultiSymbolSnapshots()
const {
  snapshots: multiSymbolSnapshots,
  loading: multiSymbolLoading,
  error: multiSymbolError,
  refresh: refreshMultiSymbols,
  start: startMultiSymbols,
} = multiSymbols
const selectedChartSymbol = ref('')
const { consumeRequestedSymbol, requestedSymbol, requestSymbol } = useSymbolStore()
const {
  pinned: pinnedSymbols,
  isPinned: symbolIsPinned,
  pin: pinSymbol,
  unpin: unpinSymbol,
} = usePinnedSymbols()
const accountRefreshIntervalMs = (window as CypressWindow).Cypress ? 500 : 10000
const { strategy, status, strategyLoading, statusLoading, loadError, load, refreshStatus } = useDashboardData()
const {
  realtimeStatus,
  connectionLabel: realtimeStatusLabel,
  connectionTagType: realtimeStatusType,
  ageSeconds,
  lastDataAt,
  reconnectNow,
} = useConnectionHealth()
// Expose the cockpit status refresh to the command palette's "refresh page".
useRegisterViewRefresh(() => {
  void refreshStatus()
})
const { account, accountError, accountLoading, accountRefreshing, refresh: refreshAccount } = useAccountRefresh(accountRefreshIntervalMs)

const router = useRouter()
const llmStatus = ref<LLMIntervalStatus | null>(null)
const recentOrders = ref<OrderRecord[]>([])
const recentEvents = ref<TradeEventRecord[]>([])
const recentNotifications = ref<NotificationLogOut[]>([])
const recentNotificationsLoading = ref(false)
const llmStatusLoading = ref(true)
const pollLoading = ref(false)
const controlInFlight = ref(false)
const recentOrdersLoading = ref(true)
const recentEventsLoading = ref(true)
const {
  history: chartHistory,
  load: loadChartHistory,
  reset: resetChartHistory,
} = useStatusHistorySeries()
const chartPoints = computed(() => chartHistory.value.points)
const tradeMarkers = computed(() => chartHistory.value.markers)
const {
  diagnostics,
  loading: diagnosticsLoading,
  load: loadDiagnostics,
} = useDiagnosticsSnapshot()
let llmStatusTimer: ReturnType<typeof setInterval> | null = null
const MAX_CHART_POINTS = 200
const MOBILE_BREAKPOINT = 768
const isMobile = ref(window.innerWidth <= MOBILE_BREAKPOINT)
const chartExpanded = ref(!isMobile.value)

function handleResize() {
  isMobile.value = window.innerWidth <= MOBILE_BREAKPOINT
}

watch(isMobile, (mobile) => {
  if (mobile) {
    chartExpanded.value = false
  }
})

const stateTagType = computed(() => {
  switch (status.value.engine_state) {
    case 'long': return 'success'
    case 'short': return 'danger'
    default: return 'info'
  }
})

const OPERATIONAL_PAUSE_PREFIXES = [
  'ORDER_SUBMISSION_UNCERTAIN:',
  'POSITION_RECONCILIATION_UNCERTAIN:',
  'REDUCTION_SETTLEMENT_UNCERTAIN:',
  'ORDER_RECONCILIATION_UNCERTAIN:',
  'ORDER_EXECUTION_BLOCKED:',
  'ORDER_PERSISTENCE_UNCERTAIN:',
  'ORDER_STATUS_PERSISTENCE_UNCERTAIN:',
]
const operationalPauseActive = computed(() =>
  status.value.paused &&
  !status.value.kill_switch &&
  OPERATIONAL_PAUSE_PREFIXES.some((prefix) =>
    diagnostics.value?.risk.pause_reason.startsWith(prefix),
  ),
)
const pausedStatusDetail = computed(() => {
  if (!status.value.runner_running) return '运行器已停止，所有自动交易已停止'
  if (!diagnostics.value) return '暂停状态确认中'
  return operationalPauseActive.value
    ? '所有交易已暂停'
    : '暂停开仓，保护性退出仍可执行'
})
const protectiveExitControlVisible = computed(() =>
  status.value.protective_exit_permitted || operationalPauseActive.value,
)

// Data-staleness watermark: once we have heard from the server at all, flag the
// cockpit when the shared status stream has gone quiet long enough that the
// displayed price / PnL may no longer reflect reality. Surfaces the same age
// the global badge uses, but inline where the numbers live.
const STALE_DATA_THRESHOLD_SECONDS = 15
const statusStale = computed(
  () => lastDataAt.value > 0 && ageSeconds.value >= STALE_DATA_THRESHOLD_SECONDS,
)
const staleAgeLabel = computed(() => relativeAgeLabel(ageSeconds.value))
// Wall-clock time of the last fresh status observation (WS frame or REST
// refresh) — the shared singleton already tracks it; surface it so the user
// can tell at a glance when the cockpit numbers were last current.
const statusUpdatedAtLabel = computed(() =>
  lastDataAt.value > 0
    ? new Date(lastDataAt.value).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
    : '—',
)

const primaryPosition = computed<Position | null>(() => {
  if (account.value.positions.length === 0) return null
  return account.value.positions.find((position) => position.symbol === strategy.value.symbol) ?? account.value.positions[0]
})

const sessionOrderHint = computed(() => {
  if (status.value.trading_session_mode !== 'RTH_ONLY') {
    return '未限制 RTH（非完整交易日历）'
  }
  return status.value.is_trading_hours
    ? 'RTH 内可下单'
    : '非 RTH，新单拦截'
})

const sessionOrderDotClass = computed(() => {
  if (status.value.trading_session_mode !== 'RTH_ONLY') return 'session-dot-neutral'
  return status.value.is_trading_hours ? 'session-dot-ok' : 'session-dot-block'
})

const drawdownLimit = computed(() =>
  status.value.max_drawdown_amount ?? strategy.value.max_drawdown_amount,
)

const chartSymbolOptions = computed(() => {
  const options: Array<{ symbol: string; label: string }> = []
  const seen = new Set<string>()
  const primarySymbol = strategy.value.symbol || ''
  if (primarySymbol) {
    options.push({ symbol: primarySymbol, label: `${primarySymbol}（主标的）` })
    seen.add(primarySymbol)
  }
  for (const item of multiSymbolSnapshots.value) {
    if (!item.symbol || seen.has(item.symbol)) continue
    const alias = item.alias ? `${item.symbol} · ${item.alias}` : item.symbol
    options.push({ symbol: item.symbol, label: alias })
    seen.add(item.symbol)
  }
  return options
})

const selectedChartIsPrimary = computed(() => selectedChartSymbol.value === (strategy.value.symbol || ''))

function syncSelectedChartSymbol() {
  const options = chartSymbolOptions.value
  if (options.length === 0) {
    selectedChartSymbol.value = strategy.value.symbol || ''
    return
  }
  // Honour a one-shot symbol request from the command palette / pinned bar.
  const requested = consumeRequestedSymbol()
  if (requested && options.some((option) => option.symbol === requested)) {
    selectedChartSymbol.value = requested
    return
  }
  if (!selectedChartSymbol.value || !options.some((option) => option.symbol === selectedChartSymbol.value)) {
    selectedChartSymbol.value = options[0].symbol
  }
}

watch(chartSymbolOptions, () => {
  syncSelectedChartSymbol()
})

// React to a live symbol request (command palette / pinned bar) even when the
// Dashboard is already mounted and the options list hasn't changed.
watch(requestedSymbol, (sym) => {
  if (sym) syncSelectedChartSymbol()
})

watch(
  () => strategy.value.symbol,
  () => {
    syncSelectedChartSymbol()
  },
)

watch(selectedChartSymbol, () => {
  void loadStatusHistory()
})

const unrealizedPnl = computed(() => {
  const position = primaryPosition.value
  if (!position || !status.value.last_price) return 0
  const priceDelta = position.side === 'SHORT'
    ? position.avg_price - status.value.last_price
    : status.value.last_price - position.avg_price
  return priceDelta * position.quantity
})

const unrealizedPnlPct = computed(() => {
  const position = primaryPosition.value
  if (!position || position.avg_price <= 0) return 0
  const priceDelta = position.side === 'SHORT'
    ? position.avg_price - status.value.last_price
    : status.value.last_price - position.avg_price
  return (priceDelta / position.avg_price) * 100
})

const priceRangePercent = computed(() => {
  const low = strategy.value.buy_low
  const high = strategy.value.sell_high
  if (low <= 0 || high <= low) return 0
  const raw = ((status.value.last_price - low) / (high - low)) * 100
  return Math.min(100, Math.max(0, raw))
})

const priceRangeLeft = computed(() => `${priceRangePercent.value}%`)
const priceRangeWidth = computed(() => `${Math.max(4, priceRangePercent.value)}%`)

async function handleRetry() {
  loadError.value = false
  try {
    await load()
    await Promise.all([refreshAccount(), loadLLMStatus(), loadRecentOrders(), loadRecentEvents(), loadRecentNotifications(), loadDiagnostics()])
    await loadStatusHistory()
  } catch {
    void 0
  }
}

async function loadLLMStatus() {
  llmStatusLoading.value = true
  try {
    llmStatus.value = await getLLMIntervalStatus()
  } catch {
    llmStatus.value = null
  } finally {
    llmStatusLoading.value = false
  }
}

async function loadRecentOrders() {
  recentOrdersLoading.value = true
  try {
    recentOrders.value = (await getOrders({ scope: 'today', page: 1, page_size: 5 })).items.slice(0, 5)
  } catch {
    recentOrders.value = []
  } finally {
    recentOrdersLoading.value = false
  }
}

async function loadRecentEvents() {
  recentEventsLoading.value = true
  try {
    recentEvents.value = (await getTradeEvents({ page: 1, page_size: 5 })).items.slice(0, 5)
  } catch {
    recentEvents.value = []
  } finally {
    recentEventsLoading.value = false
  }
}

async function loadRecentNotifications() {
  recentNotificationsLoading.value = true
  try {
    const data = await getNotifications({ page: 1, page_size: 5 })
    recentNotifications.value = data.items.slice(0, 5)
  } catch {
    recentNotifications.value = []
  } finally {
    recentNotificationsLoading.value = false
  }
}

async function loadStatusHistory() {
  const symbol = selectedChartSymbol.value || strategy.value.symbol || ''
  if (!symbol) {
    resetChartHistory()
    return
  }
  try {
    const result = await loadChartHistory({ limit: MAX_CHART_POINTS, symbol })
    chartHistory.value = {
      points: result.points.slice(-MAX_CHART_POINTS),
      markers: result.markers,
    }
  } catch {
    resetChartHistory()
  }
}

const metricsWindowDays = ref(30)
const metricsLoading = ref(false)
interface DashboardMetrics {
  trade_count: number
  win_rate: number
  profit_factor: number | null
  sharpe_ratio: number | null
  avg_pnl: number
  max_drawdown: number
}
const metrics = ref<DashboardMetrics>({
  trade_count: 0,
  win_rate: 0,
  profit_factor: null,
  sharpe_ratio: null,
  avg_pnl: 0,
  max_drawdown: 0,
})

async function loadMetrics() {
  metricsLoading.value = true
  try {
    const data = await getMetricsSummary({ days: metricsWindowDays.value })
    metrics.value = {
      trade_count: data.trade_count ?? 0,
      win_rate: data.win_rate ?? 0,
      profit_factor: data.profit_factor ?? null,
      sharpe_ratio: data.sharpe_ratio ?? null,
      avg_pnl: data.avg_pnl ?? 0,
      max_drawdown: data.max_drawdown ?? 0,
    }
  } catch {
    // Leave previous values in place; the panel shows a load error via
    // the section's v-loading state.
  } finally {
    metricsLoading.value = false
  }
}

function appendStatusPoint() {
  const primarySymbol = strategy.value.symbol || ''
  if (!primarySymbol || selectedChartSymbol.value !== primarySymbol) {
    return
  }
  const now = new Date().toISOString()
  const points = chartHistory.value.points
  const previous = points[points.length - 1]
  const point: StatusHistoryPoint = {
    symbol: primarySymbol,
    timestamp: now,
    engine_state: status.value.engine_state,
    paused: status.value.paused,
    kill_switch: status.value.kill_switch,
    daily_pnl: status.value.daily_pnl,
    consecutive_losses: status.value.consecutive_losses,
    last_price: status.value.last_price,
    last_trigger_price: status.value.last_trigger_price,
  }
  if (
    previous
    && previous.last_price === point.last_price
    && previous.daily_pnl === point.daily_pnl
    && previous.engine_state === point.engine_state
    && previous.symbol === point.symbol
  ) {
    return
  }
  chartHistory.value = {
    points: [...points, point].slice(-MAX_CHART_POINTS),
    markers: chartHistory.value.markers,
  }
}

onMounted(() => {
  loadLLMStatus()
  loadRecentOrders()
  loadRecentEvents()
  loadRecentNotifications()
  loadStatusHistory()
  loadMetrics()
  loadDiagnostics()
  startMultiSymbols()
  llmStatusTimer = setInterval(() => {
    if (pollLoading.value) return
    pollLoading.value = true
    Promise.all([
      loadLLMStatus(),
      loadRecentOrders(),
      loadRecentEvents(),
      loadRecentNotifications(),
      loadDiagnostics(),
    ]).finally(() => { pollLoading.value = false })
  }, 5000)
  load().catch(() => void 0)
  window.addEventListener('resize', handleResize)
})

watch(
  () => [
    status.value.last_price,
    status.value.daily_pnl,
    status.value.engine_state,
    status.value.paused,
    status.value.kill_switch,
    status.value.protective_exit_permitted,
  ],
  appendStatusPoint,
)

onUnmounted(() => {
  if (llmStatusTimer) {
    clearInterval(llmStatusTimer)
    llmStatusTimer = null
  }
  window.removeEventListener('resize', handleResize)
})

async function runControlAction(action: () => Promise<unknown>, successMessage: string, errorMessage: string) {
  if (controlInFlight.value) return
  controlInFlight.value = true
  try {
    await action()
    ElMessage.success(successMessage)
    await Promise.all([refreshStatus(), loadDiagnostics()])
  } catch {
    ElMessage.error(errorMessage)
  } finally {
    controlInFlight.value = false
  }
}

async function handleStart() {
  await runControlAction(startTrading, '交易已启动', '启动失败')
}

async function handleStop() {
  await runControlAction(stopTrading, '交易已停止', '停止失败')
}

async function handlePause() {
  await runControlAction(pauseTrading, '交易已暂停', '暂停失败')
}

async function handleResume() {
  await runControlAction(resumeTrading, '交易已恢复', '恢复失败')
}

async function handleProtectiveExits() {
  if (controlInFlight.value) return
  if (status.value.protective_exit_permitted) {
    await runControlAction(disableProtectiveExits, '仅减仓许可已撤销', '撤销仅减仓许可失败')
    return
  }
  try {
    await ElMessageBox.confirm(
      '启用后系统仍保持暂停，但可立即提交止损或减仓订单，最多卖出当前全部可用持仓。确定继续？',
      '启用仅减仓许可',
      { type: 'warning', confirmButtonText: '确认启用' },
    )
  } catch {
    return
  }
  controlInFlight.value = true
  try {
    await enableProtectiveExits()
    ElMessage.success('仅减仓许可已启用')
    await Promise.all([refreshStatus(), loadDiagnostics()])
  } catch (error) {
    const message = resolveErrorMessage(error, '启用仅减仓许可失败')
    if (message.includes('first coherent empty broker proof')) {
      ElMessage.warning('第一轮券商校验已通过，请至少等待 5 秒后再次确认')
    } else {
      ElMessage.error(message)
    }
  } finally {
    controlInFlight.value = false
  }
}

async function handleKillSwitch() {
  try {
    await ElMessageBox.confirm('确定要触发紧急停止吗？', '紧急停止', { type: 'warning' })
  } catch {
    return
  }
  try {
    await activateKillSwitch()
    ElMessage.success('紧急停止已触发')
    await Promise.all([refreshStatus(), loadDiagnostics()])
  } catch {
    ElMessage.error('紧急停止触发失败')
  }
}

async function handleDisableKillSwitch() {
  await runControlAction(disableKillSwitch, '紧急停止已解除', '解除失败')
}

function formatAgeSeconds(value: number | null | undefined): string {
  if (value == null) return '-'
  return `${value.toFixed(1)}s`
}

/** Dump the in-memory diagnostics snapshot (runtime table + stream health) to
 * CSV for incident review. Reuses the already-polled diagnostics state only. */
function exportDiagnostics() {
  const d = diagnostics.value
  if (!d) return
  const rows = d.symbol_runtimes.map((r) => ({
    symbol: r.symbol,
    market: r.market,
    is_primary: r.is_primary ? 'yes' : 'no',
    engine_state: r.engine_state,
    last_price: r.last_price,
    last_trigger_price: r.last_trigger_price,
    recent_quote_count: r.recent_quote_count,
    has_pending_order: r.has_pending_order ? 'yes' : 'no',
    position_quantity: r.position_quantity,
    position_avg_price: r.position_avg_price,
    position_notional: r.position_notional,
    position_risk_at_stop: r.position_risk_at_stop,
    position_limit_breaches: r.position_limit_breaches.join('|'),
  }))
  downloadCsv('diagnostics_snapshot.csv', [
    { key: 'symbol', label: 'symbol' },
    { key: 'market', label: 'market' },
    { key: 'is_primary', label: 'is_primary' },
    { key: 'engine_state', label: 'engine_state' },
    { key: 'last_price', label: 'last_price' },
    { key: 'last_trigger_price', label: 'last_trigger_price' },
    { key: 'recent_quote_count', label: 'recent_quote_count' },
    { key: 'has_pending_order', label: 'has_pending_order' },
    { key: 'position_quantity', label: 'position_quantity' },
    { key: 'position_avg_price', label: 'position_avg_price' },
    { key: 'position_notional', label: 'position_notional' },
    { key: 'position_risk_at_stop', label: 'position_risk_at_stop' },
    { key: 'position_limit_breaches', label: 'position_limit_breaches' },
  ], rows)
  ElMessage.success(`已导出 ${rows.length} 个运行时诊断`)
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
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

function formatCooldown(seconds: number | null | undefined): string {
  if (seconds == null || seconds <= 0) return '-'
  return `${Math.ceil(seconds)}s`
}

function pnlLabel(value: number | null | undefined): string {
  const normalized = value ?? 0
  if (normalized > 0) return '盈利'
  if (normalized < 0) return '亏损'
  return '持平'
}

function metricClass(value: number | null | undefined): string {
  const normalized = value ?? 0
  if (normalized > 0) return 'metric-positive'
  if (normalized < 0) return 'metric-negative'
  return ''
}

function eventTagType(
  eventTypeValue: string,
  evtStatus: string,
  source?: TradeEventRecord['source'],
): string {
  if (source === 'audit') return eventTypeValue === 'KILL_SWITCH' ? 'danger' : 'info'
  if (eventTypeValue === EVENT_TYPE.LLM_ANALYSIS) return evtStatus === 'FAILED' ? 'danger' : 'primary'
  if (eventTypeValue === 'RISK_PAUSED') return 'danger'
  if (eventTypeValue === 'RISK_AUTO_RESUMED') return 'success'
  if (eventTypeValue === 'ORDER_FILLED') return 'success'
  if (eventTypeValue === 'ORDER_CANCELLED') return 'info'
  if (eventTypeValue === 'ORDER_REJECTED') return 'danger'
  if (eventTypeValue === 'ORDER_SKIPPED') return 'warning'
  return 'warning'
}

function severityType(s: string): string {
  if (s === 'CRITICAL') return 'danger'
  if (s === 'WARNING') return 'warning'
  return 'info'
}
</script>

<style scoped>
.dashboard-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.dashboard-alert {
  margin-bottom: 0;
}

.page-heading,
.cockpit-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-heading h3 {
  margin: 0;
}

.page-heading p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.heading-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.notif-ticker {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 12px 0;
  border-radius: 6px;
  padding: 8px 12px;
  background: #fff;
  border: 1px solid #e1e7f0;
}

.ticker-label {
  flex-shrink: 0;
  color: #6b7280;
  font-size: 12px;
  font-weight: 700;
}

.ticker-items {
  display: flex;
  align-items: center;
  gap: 16px;
  flex: 1;
  min-width: 0;
  overflow: hidden;
}

.ticker-item {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  white-space: nowrap;
}

.ticker-title {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 220px;
  color: #172033;
  font-size: 13px;
}

.dashboard-cockpit {
  border: 1px solid #dfe5ee;
  border-radius: 8px;
  padding: 16px;
  background: #f7f9fc;
}

.status-strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin: 14px 0;
}

.strip-item,
.cockpit-panel,
.detail-panel {
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  background: #fff;
}

.strip-item {
  min-height: 72px;
  padding: 10px 12px;
}

.strip-item span,
.mini-grid span,
.position-main span,
.pnl-box span,
.strategy-list span {
  display: block;
  color: #6b7280;
  font-size: 12px;
}

.strip-item strong {
  display: block;
  margin-top: 4px;
  color: #172033;
  font-size: 18px;
  line-height: 1.1;
}

.strip-item small {
  display: block;
  margin-top: 4px;
  color: #7a8595;
  font-size: 12px;
}

.session-strip-hint {
  display: flex !important;
  align-items: center;
  gap: 6px;
}

.panel-caption {
  margin: 4px 0 0;
  color: #6b7280;
  font-size: 12px;
}

.session-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.session-dot-ok {
  background: #22c55e;
}

.session-dot-block {
  background: #94a3b8;
}

.session-dot-neutral {
  background: #cbd5e1;
}

.cockpit-stale-alert {
  margin-bottom: 12px;
  cursor: pointer;
}

.heading-updated {
  color: #909399;
  font-size: 12px;
  white-space: nowrap;
}

.pinned-symbols {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.pinned-label {
  color: #909399;
  font-size: 12px;
}
.pinned-chip {
  cursor: pointer;
}

.cockpit-grid {
  display: grid;
  grid-template-columns: minmax(280px, 1.25fr) minmax(260px, 1fr) minmax(260px, 1fr) minmax(250px, .95fr);
  gap: 10px;
}

.cockpit-panel {
  min-height: 248px;
  padding: 14px;
}

.panel-heading,
.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.panel-heading span,
.section-title h4,
.subsection-title {
  margin: 0;
  color: #4b5563;
  font-size: 13px;
  font-weight: 700;
}

.price-value {
  margin: 14px 0 12px;
  color: #111827;
  font-size: 44px;
  font-weight: 800;
  line-height: 1;
}

.range-line {
  position: relative;
  height: 10px;
  border-radius: 999px;
  background: #edf1f7;
  overflow: hidden;
}

.range-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: #409eff;
}

.range-marker {
  position: absolute;
  top: -3px;
  width: 4px;
  height: 16px;
  border-radius: 999px;
  background: #172033;
  transform: translateX(-50%);
}

.range-labels {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
  color: #6b7280;
  font-size: 12px;
}

.diagnostics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}

.diagnostic-block {
  border-radius: 6px;
  padding: 10px;
  background: #f8fafc;
}

.diagnostic-block span,
.diagnostic-block small {
  display: block;
  color: #6b7280;
  font-size: 12px;
}

.diagnostic-block strong {
  display: block;
  margin-top: 4px;
  color: #172033;
  font-size: 16px;
}

.live-safety {
  margin-bottom: 14px;
}

.live-safety h5 {
  margin: 0 0 8px;
  color: #334155;
  font-size: 13px;
  font-weight: 600;
}

.safety-inline-note {
  margin-left: 8px;
  color: #64748b;
  font-size: 12px;
}

.mini-grid,
.position-main {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 14px;
}

.mini-grid div,
.position-main div,
.pnl-box {
  border-radius: 6px;
  padding: 10px;
  background: #f8fafc;
}

.mini-grid strong,
.position-main strong,
.pnl-box strong {
  display: block;
  margin-top: 3px;
  color: #172033;
  font-size: 18px;
}

.action-message {
  grid-column: 1 / -1;
}

.action-message strong {
  font-size: 13px;
  line-height: 1.45;
}

.position-symbol {
  margin-top: 12px;
  color: #111827;
  font-size: 28px;
  font-weight: 800;
}

.position-main {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.pnl-box {
  margin-top: 10px;
}

.llm-decision {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 14px;
}

.llm-decision strong {
  color: #111827;
  font-size: 36px;
  line-height: 1;
}

.llm-decision span,
.llm-panel small {
  color: #6b7280;
  font-size: 12px;
}

.llm-range {
  margin: 12px 0 8px;
  color: #172033;
  font-weight: 700;
}

.llm-analysis {
  display: -webkit-box;
  min-height: 44px;
  margin: 0 0 10px;
  overflow: hidden;
  color: #4b5563;
  font-size: 13px;
  line-height: 1.55;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.llm-meta,
.llm-apply-state {
  display: grid;
  gap: 4px;
  color: #6b7280;
  font-size: 12px;
}

.llm-apply-state {
  margin-top: 8px;
}

.llm-apply-state span {
  line-height: 1.4;
}

.llm-schedule {
  margin-top: 14px;
  border-top: 1px solid #e1e7f0;
  padding-top: 12px;
}

.llm-budget-bar {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 10px;
}

.budget-cell {
  border-radius: 6px;
  padding: 8px;
  background: #f8fafc;
  text-align: center;
}

.budget-cell span {
  display: block;
  color: #6b7280;
  font-size: 11px;
}

.budget-cell strong {
  display: block;
  margin-top: 3px;
  color: #172033;
  font-size: 16px;
}

.llm-symbol-status .subsection-title {
  margin-top: 0;
  margin-bottom: 8px;
}

.llm-skip-reason {
  display: block;
  margin-top: 2px;
  color: #f59e0b;
  font-size: 11px;
  line-height: 1.3;
}

.action-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 14px;
}

.action-grid :deep(.el-button) {
  width: 100%;
  margin-left: 0;
}

.kill-button {
  grid-column: 1 / -1;
}

.protective-exit-button {
  grid-column: 1 / -1;
}

.chart-grid {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chart-panels {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
  gap: 12px;
}

.chart-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.chart-symbol-controls {
  display: flex;
  align-items: center;
  gap: 12px;
}

.detail-grid {
  display: grid;
  grid-template-columns: 1.05fr 1fr 1.05fr 1.15fr;
  gap: 12px;
}

.detail-panel {
  padding: 14px;
}

.section-title h4 {
  color: #172033;
  font-size: 15px;
}

.section-title strong,
.section-title span {
  color: #172033;
  font-weight: 800;
}

.subsection-title {
  margin: 14px 0 8px;
}

.strategy-list,
.risk-list {
  display: grid;
  gap: 8px;
}

.strategy-list {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.strategy-list div {
  border-radius: 6px;
  padding: 8px;
  background: #f8fafc;
}

.strategy-list strong {
  display: block;
  margin-top: 4px;
}

.risk-list {
  color: #4b5563;
  font-size: 13px;
}

.event-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.event-row {
  border-radius: 6px;
  padding: 9px;
  background: #f8fafc;
}

.event-main {
  display: grid;
  grid-template-columns: auto minmax(80px, 1fr) auto;
  align-items: center;
  gap: 8px;
}

.event-main strong {
  overflow: hidden;
  color: #172033;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.event-main small {
  color: #7a8595;
  font-size: 11px;
  white-space: nowrap;
}

.event-row p {
  display: -webkit-box;
  margin: 7px 0 0;
  overflow: hidden;
  color: #4b5563;
  font-size: 12px;
  line-height: 1.45;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.skip-category {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.empty-note {
  margin: 24px 0;
  color: #999;
  text-align: center;
}

.metric-positive {
  color: #14884f !important;
}

.metric-negative {
  color: #c43838 !important;
}

.responsive-table {
  width: 100%;
}

@media (max-width: 1280px) {
  .status-strip {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .cockpit-grid,
  .chart-panels,
  .detail-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 768px) {
  .page-heading,
  .cockpit-heading {
    flex-direction: column;
    gap: 10px;
  }

  .heading-tags {
    justify-content: flex-start;
  }

  .status-strip,
  .cockpit-grid,
  .chart-panels,
  .strategy-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .status-strip {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }

  .detail-grid {
    grid-template-columns: 1fr;
  }

  .dashboard-cockpit,
  .cockpit-panel {
    padding: 12px;
  }

  .strip-item {
    min-height: 64px;
    padding: 8px;
  }

  .strip-item strong {
    font-size: 16px;
  }

  .cockpit-panel {
    min-height: 210px;
  }

  .price-value {
    font-size: 36px;
  }

  .position-main {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .action-grid :deep(.el-button) {
    min-height: 44px;
    font-size: 14px;
  }

  .chart-panels {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 520px) {
  .notif-ticker {
    gap: 8px;
  }

  .ticker-item {
    flex: 1;
    min-width: 0;
  }

  .ticker-item:not(:first-child) {
    display: none;
  }

  .ticker-title {
    display: block;
    flex: 1;
    min-width: 0;
    max-width: none;
  }

  .ticker-item small {
    display: none;
  }

  .status-strip,
  .cockpit-grid,
  .chart-panels,
  .position-main,
  .strategy-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .cockpit-grid,
  .chart-panels,
  .position-main,
  .strategy-list {
    grid-template-columns: 1fr;
  }

  .action-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .action-grid :deep(.el-button) {
    min-height: 48px;
    font-size: 15px;
  }

  .kill-button {
    min-height: 52px;
    font-size: 16px;
  }
}
</style>
