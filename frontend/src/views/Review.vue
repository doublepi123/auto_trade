<template>
  <div data-testid="review-page-root" class="review-page" :class="{ 'review-compact': compactReviewMode }">
    <div class="review-header">
      <div>
        <h3>复盘工作台</h3>
        <p>分析 LLM 建议与真实交易结果，优化策略决策</p>
      </div>
    </div>

    <el-card class="filter-card">
      <el-form :inline="true" @submit.prevent="handleSearch">
        <el-form-item label="股票代码">
          <el-input v-model="form.symbol" placeholder="例如 AAPL.US" style="width: 160px" />
        </el-form-item>
        <el-form-item label="开始日期">
          <el-date-picker v-model="form.from_date" type="date" value-format="YYYY-MM-DD" style="width: 140px" />
        </el-form-item>
        <el-form-item label="结束日期">
          <el-date-picker v-model="form.to_date" type="date" value-format="YYYY-MM-DD" style="width: 140px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="handleSearch">查询</el-button>
          <el-button-group>
            <el-button data-testid="review-quick-7d" @click="setQuickRange(7)">近7天</el-button>
            <el-button data-testid="review-quick-30d" @click="setQuickRange(30)">近30天</el-button>
            <el-button data-testid="review-quick-90d" @click="setQuickRange(90)">近90天</el-button>
          </el-button-group>
          <el-button plain :disabled="!reviewData || reviewData.days.length === 0" @click="handleExport('json')">导出 JSON</el-button>
          <el-button plain :disabled="!reviewData || reviewData.days.length === 0" @click="handleExport('csv')">导出 CSV</el-button>
          <el-button
            data-testid="review-export-visible-csv"
            plain
            :disabled="visibleTimelineRows.length === 0"
            @click="exportVisibleTimelineCsv"
          >
            导出当前筛选 CSV
          </el-button>
          <el-button data-testid="review-copy-brief" plain :disabled="!reviewBriefText" @click="copyReviewBrief">复制摘要</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="llm-status-card" data-testid="review-llm-status-card" v-loading="llmStatusLoading">
      <template #header>
        <div class="llm-status-header">
          <div>
            <strong>LLM 实时状态</strong>
            <p>{{ form.symbol || '当前标的' }}</p>
          </div>
          <el-tag v-if="llmStatus" :type="llmStatus.enabled ? 'success' : 'info'" effect="plain">
            {{ llmStatus.enabled ? '已启用' : '已禁用' }}
          </el-tag>
        </div>
      </template>

      <template v-if="llmStatus">
        <div class="llm-status-meta">
          <span>分析间隔：{{ llmStatus.interval_minutes }} 分钟</span>
          <span>下次分析：{{ llmStatus.next_analysis_at ? formatDateTime(llmStatus.next_analysis_at) : '-' }}</span>
        </div>
        <div class="llm-budget-bar" data-testid="review-llm-budget-bar">
          <div class="budget-cell" data-testid="review-llm-budget-tracked">
            <span>本周期标的</span>
            <strong>{{ (llmStatus.budget?.tracked_symbol_count ?? 0) }}/{{ (llmStatus.budget?.max_symbols_per_cycle ?? 0) }}</strong>
          </div>
          <div class="budget-cell" data-testid="review-llm-budget-hourly">
            <span>小时分析</span>
            <strong>{{ (llmStatus.budget?.used_analyses_last_hour ?? 0) }}/{{ (llmStatus.budget?.max_analyses_per_hour ?? 0) }}</strong>
          </div>
          <div class="budget-cell" data-testid="review-llm-budget-remaining">
            <span>剩余次数</span>
            <strong>{{ llmStatus.budget?.remaining_analyses_this_hour ?? 0 }}</strong>
          </div>
        </div>

        <div v-if="selectedSymbolLLMStatus" class="selected-symbol-llm-status" data-testid="review-selected-symbol-llm-status">
          <div class="section-title">{{ selectedSymbolLLMStatus.symbol }} 调度状态</div>
          <div class="item-row">
            <span>角色</span>
            <el-tag :type="selectedSymbolLLMStatus.is_primary ? 'success' : 'info'" size="small">
              {{ selectedSymbolLLMStatus.is_primary ? '主标的' : '观察标的' }}
            </el-tag>
          </div>
          <div class="item-row">
            <span>挂单</span>
            <strong>{{ selectedSymbolLLMStatus.has_pending_order ? '存在' : '无' }}</strong>
          </div>
          <div class="item-row">
            <span>下次分析</span>
            <strong>{{ selectedSymbolLLMStatus.next_analysis_at ? formatDateTime(selectedSymbolLLMStatus.next_analysis_at) : '-' }}</strong>
          </div>
          <div class="item-row">
            <span>买入冷却</span>
            <strong>{{ formatCooldown(selectedSymbolLLMStatus.buy_cooldown_remaining_seconds) }}</strong>
          </div>
          <div class="item-row">
            <span>卖出冷却</span>
            <strong>{{ formatCooldown(selectedSymbolLLMStatus.sell_cooldown_remaining_seconds) }}</strong>
          </div>
          <div v-if="selectedSymbolLLMStatus.last_skip_reason" class="item-row">
            <span>最近跳过</span>
            <small class="skip-reason">{{ selectedSymbolLLMStatus.last_status }}：{{ selectedSymbolLLMStatus.last_skip_reason }}</small>
          </div>
        </div>
      </template>
      <p v-else class="empty-note">暂无 LLM 状态</p>
    </el-card>

    <el-card class="risk-history-card" data-testid="review-risk-history">
      <template #header>
        <div class="runtime-history-header">
          <div>
            <strong>风险历史</strong>
            <p>日内盈亏与连续亏损趋势</p>
          </div>
        </div>
      </template>
      <RiskHistoryPanel />
    </el-card>

    <template v-if="reviewData && reviewData.days.length > 0">
      <el-row :gutter="12" class="summary-row">
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value" :class="pnlClass">{{ signedCurrency(reviewData.total_pnl) }}</div>
            <div class="summary-label">总盈亏</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ reviewData.total_trades }}</div>
            <div class="summary-label">总交易次数</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ reviewData.days.length }}</div>
            <div class="summary-label">复盘天数</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value error-tags">
              <el-tag v-for="tag in reviewData.all_error_tags" :key="tag" type="warning" size="small">{{ tag }}</el-tag>
              <span v-if="reviewData.all_error_tags.length === 0" class="no-tag">无</span>
            </div>
            <div class="summary-label">错误标签</div>
          </el-card>
        </el-col>
      </el-row>

      <el-card class="review-health-card" data-testid="review-health-score">
        <template #header>
          <div class="runtime-history-header">
            <div>
              <strong>复盘健康</strong>
              <p>当前查询结果 · 诊断分，不是交易建议</p>
            </div>
            <el-tag :type="reviewHealth.tagType" effect="plain">{{ reviewHealth.label }}</el-tag>
          </div>
        </template>

        <div class="review-health-body">
          <div class="review-health-score-value">{{ reviewHealth.score }}</div>
          <div class="review-health-score-label">{{ reviewHealth.label }}</div>
          <div v-if="reviewHealth.reasons.length > 0" data-testid="review-health-reasons" class="review-health-reasons">
            <el-tag v-for="reason in reviewHealth.reasons" :key="reason" size="small" effect="plain">{{ reason }}</el-tag>
          </div>
        </div>
      </el-card>

      <el-card class="runtime-history-card" data-testid="review-runtime-history">
        <template #header>
          <div class="runtime-history-header">
            <div>
              <strong>运行时状态历史</strong>
              <p>{{ reviewSymbolLabel }}</p>
            </div>
            <el-tag size="small" type="info">{{ runtimeHistory.points.length }} 个样本</el-tag>
          </div>
        </template>

        <el-alert
          v-if="runtimeHistoryError"
          :title="runtimeHistoryError"
          type="warning"
          :closable="false"
          style="margin-bottom: 12px"
        />

        <el-empty
          v-if="!runtimeHistoryLoading && runtimeHistory.points.length === 0"
          description="当前条件下暂无运行时状态历史"
        />
        <div v-else class="chart-grid">
          <PriceChart :points="runtimeHistory.points" :markers="runtimeHistory.markers" :buy-low="0" :sell-high="0" />
          <PnLChart :points="runtimeHistory.points" />
        </div>
      </el-card>

      <el-card class="runtime-history-card" data-testid="review-diagnostics">
        <template #header>
          <div class="runtime-history-header">
            <div>
              <strong>运行诊断快照</strong>
              <p>{{ diagnosticsSymbolLabel }}</p>
            </div>
            <el-tag size="small" :type="selectedRuntimeDiagnostics?.has_pending_order ? 'warning' : 'success'">
              {{ selectedRuntimeDiagnostics?.has_pending_order ? '存在挂单' : '无挂单' }}
            </el-tag>
          </div>
        </template>

        <el-alert
          v-if="diagnosticsError"
          :title="diagnosticsError"
          type="warning"
          :closable="false"
          style="margin-bottom: 12px"
        />

        <template v-else-if="diagnostics && selectedRuntimeDiagnostics">
          <div class="chart-grid diagnostics-summary-grid">
            <div class="section-block">
              <div class="section-title">运行态</div>
              <div class="item-row">
                <span>{{ selectedRuntimeDiagnostics.symbol }} · {{ selectedRuntimeDiagnostics.engine_state }}</span>
                <el-tag :type="selectedRuntimeDiagnostics.is_primary ? 'success' : 'info'" size="small">
                  {{ selectedRuntimeDiagnostics.is_primary ? '主标的' : '观察标的' }}
                </el-tag>
              </div>
              <div class="item-row">
                <span>最近价格 {{ formatCurrency(selectedRuntimeDiagnostics.last_price ?? null, diagnosticsMarket) }}</span>
                <span>触发价 {{ selectedRuntimeDiagnostics.last_trigger_price > 0 ? formatCurrency(selectedRuntimeDiagnostics.last_trigger_price ?? null, diagnosticsMarket) : '-' }}</span>
              </div>
            </div>

            <div class="section-block">
              <div class="section-title">流与线程</div>
              <div class="item-row">
                <span>线程存活</span>
                <strong>{{ diagnostics.thread_alive ? '是' : '否' }}</strong>
              </div>
              <div class="item-row">
                <span>最近推送 {{ formatAgeSeconds(diagnostics.quote_stream.last_push_age_seconds) }}</span>
                <span>最近报价 {{ formatAgeSeconds(diagnostics.quote_stream.last_quote_age_seconds) }}</span>
              </div>
            </div>
          </div>
        </template>
        <p v-else class="empty-note">当前 symbol 暂无运行诊断快照</p>
      </el-card>

      <el-card class="runtime-history-card" data-testid="review-snapshot-strip">
        <template #header>
          <div class="runtime-history-header">
            <div>
              <strong>快照波动（当前筛选结果）</strong>
              <p>基于当前可见复盘日统计</p>
            </div>
            <el-tag size="small" type="info">{{ snapshotSummary.count }} 样本</el-tag>
          </div>
        </template>

        <template v-if="snapshotSummary.count > 0">
          <div class="chart-grid diagnostics-summary-grid">
            <div class="section-block">
              <div class="section-title">价格区间</div>
              <div class="item-row">
                <span>样本 {{ snapshotSummary.count }}</span>
                <span>最低 {{ snapshotSummary.minPrice ?? '-' }}</span>
                <span>最高 {{ snapshotSummary.maxPrice ?? '-' }}</span>
              </div>
            </div>

            <div class="section-block">
              <div class="section-title">波动与状态</div>
              <div class="item-row">
                <span>最新状态 {{ snapshotSummary.latestState || '-' }}</span>
                <span>最大连亏 {{ snapshotSummary.maxConsecutiveLosses ?? '-' }}</span>
                <span>触发距离 {{ snapshotSummary.maxTriggerDistance ?? '-' }}</span>
              </div>
            </div>
          </div>
        </template>
        <p v-else class="empty-note">无快照样本</p>
      </el-card>

      <el-card class="runtime-history-card" data-testid="review-llm-action-summary">
        <template #header>
          <div class="runtime-history-header">
            <div>
              <strong>LLM 动作摘要（当前筛选结果）</strong>
              <p>基于当前可见复盘日统计</p>
            </div>
            <el-tag size="small" type="info">{{ llmActionSummary.total }} 条</el-tag>
          </div>
        </template>

        <div class="chart-grid diagnostics-summary-grid">
          <div class="section-block">
            <div class="section-title">动作分布</div>
            <div class="item-row">
              <el-tag v-for="action in llmActionSummary.actions" :key="action" size="small" effect="plain">
                {{ action }} {{ llmActionSummary.actionCounts[action] }}
              </el-tag>
            </div>
          </div>

          <div class="section-block">
            <div class="section-title">结果统计</div>
            <div class="item-row">
              <span>成功 {{ llmActionSummary.success }}/{{ llmActionSummary.total }}</span>
              <span>已应用 {{ llmActionSummary.applied }}</span>
              <span>关联订单 {{ llmActionSummary.linked }}</span>
            </div>
          </div>
        </div>
      </el-card>

      <el-card class="runtime-history-card" data-testid="review-event-buckets">
        <template #header>
          <div class="runtime-history-header">
            <div>
              <strong>事件分桶（当前筛选结果）</strong>
              <p>基于当前可见复盘日统计</p>
            </div>
            <el-tag size="small" type="info">总计 {{ eventBucketSummary.total }} 条</el-tag>
          </div>
        </template>

        <div class="chart-grid diagnostics-summary-grid">
          <div class="section-block">
            <div class="section-title">分桶分布</div>
            <div class="item-row">
              <span v-for="bucket in EVENT_BUCKETS" :key="bucket">{{ EVENT_BUCKET_LABELS[bucket] }} {{ eventBucketSummary[bucket] }}</span>
            </div>
          </div>

          <div class="section-block">
            <div class="section-title">总计</div>
            <div class="item-row">
              <span>总计 {{ eventBucketSummary.total }}</span>
            </div>
          </div>
        </div>
      </el-card>

      <el-card class="runtime-history-card" data-testid="review-execution-quality">
        <template #header>
          <div class="runtime-history-header">
            <div>
              <strong>订单执行质量（当前筛选结果）</strong>
              <p>基于当前可见复盘日统计</p>
            </div>
            <el-tag size="small" type="info">{{ executionQuality.slippageSamples }} 个滑点样本</el-tag>
          </div>
        </template>

        <div class="chart-grid diagnostics-summary-grid">
          <div class="section-block">
            <div class="section-title">状态分布</div>
            <div class="item-row">
              <span>成交 {{ executionQuality.filled }}</span>
              <span>部分 {{ executionQuality.partial }}</span>
              <span>挂起 {{ executionQuality.open }}</span>
              <span>异常 {{ executionQuality.failed }}</span>
            </div>
          </div>

          <div class="section-block">
            <div class="section-title">滑点统计</div>
            <div class="item-row">
              <span>平均价差(成交-委托) {{ executionQuality.avgSlippage }}</span>
              <span>样本 {{ executionQuality.slippageSamples }}</span>
            </div>
          </div>
        </div>
      </el-card>

      <div class="timeline-section">
        <div data-testid="review-workbench-toolbar" class="review-workbench-toolbar">
          <div class="workbench-toolbar-summary">
            <strong>可见复盘日</strong>
            <span data-testid="review-visible-day-count">
              当前筛选结果：{{ filteredReviewDays.length }} / {{ reviewData?.days.length ?? 0 }} 天
            </span>
          </div>
          <el-input
            v-model="timelineKeyword"
            data-testid="review-keyword-filter"
            placeholder="搜索当前复盘：订单/事件/LLM/快照"
            clearable
            style="width: 260px"
          />
          <el-button data-testid="review-compact-mode" :type="compactReviewMode ? 'primary' : ''" @click="compactReviewMode = !compactReviewMode">
            紧凑模式
          </el-button>
          <el-button-group>
            <el-button data-testid="review-day-filter-all" :type="dayFilter === 'all' ? 'primary' : ''" @click="dayFilter = 'all'">all</el-button>
            <el-button data-testid="review-day-filter-losing" :type="dayFilter === 'losing' ? 'primary' : ''" @click="dayFilter = 'losing'">losing</el-button>
            <el-button data-testid="review-day-filter-winning" :type="dayFilter === 'winning' ? 'primary' : ''" @click="dayFilter = 'winning'">winning</el-button>
            <el-button data-testid="review-day-filter-error" :type="dayFilter === 'error' ? 'primary' : ''" @click="dayFilter = 'error'">error</el-button>
            <el-button data-testid="review-day-filter-traded" :type="dayFilter === 'traded' ? 'primary' : ''" @click="dayFilter = 'traded'">traded</el-button>
            <el-button data-testid="review-day-filter-no-trade" :type="dayFilter === 'no_trade' ? 'primary' : ''" @click="dayFilter = 'no_trade'">no_trade</el-button>
            <el-button data-testid="review-day-filter-llm" :type="dayFilter === 'llm' ? 'primary' : ''" @click="dayFilter = 'llm'">llm</el-button>
            <el-button data-testid="review-day-filter-event" :type="dayFilter === 'event' ? 'primary' : ''" @click="dayFilter = 'event'">event</el-button>
          </el-button-group>
          <el-button data-testid="review-reset-workbench-filters" @click="resetWorkbenchFilters">reset</el-button>
        </div>

        <el-empty v-if="filteredReviewDays.length === 0" data-testid="review-filtered-empty">
          <template #description>当前筛选条件下无复盘日</template>
        </el-empty>

        <div v-for="day in filteredReviewDays" :key="day.date" class="day-card" :data-testid="`review-day-card-${day.date}`">
          <div class="day-header">
            <strong>{{ day.date }}</strong>
            <el-tag :type="day.daily_pnl >= 0 ? 'success' : 'danger'" effect="plain">{{ signedCurrency(day.daily_pnl) }}</el-tag>
            <el-tag v-if="day.trade_count > 0" type="info" effect="plain">{{ day.trade_count }} 笔交易</el-tag>
            <div class="day-tags">
              <el-tag v-for="tag in day.error_tags" :key="tag" type="warning" size="small">{{ tag }}</el-tag>
            </div>
          </div>

          <div class="day-meta">
            <div data-testid="review-day-composition" class="day-meta-row">
              <span>LLM {{ day.llm_interactions.length }}</span>
              <span>订单 {{ day.orders.length }}</span>
              <span>事件 {{ day.events.length }}</span>
              <span>快照 {{ day.snapshots.length }}</span>
              <span>错误 {{ day.error_tags.length }}</span>
            </div>
            <div data-testid="review-day-state" class="day-meta-row">
              <el-tag :type="day.daily_pnl > 0 ? 'success' : day.daily_pnl < 0 ? 'danger' : 'info'" size="small" effect="plain">
                {{ day.daily_pnl > 0 ? '盈利' : day.daily_pnl < 0 ? '亏损' : '打平' }}
              </el-tag>
              <el-tag :type="day.trade_count > 0 ? 'success' : 'info'" size="small" effect="plain">
                {{ day.trade_count > 0 ? '有交易' : '无交易' }}
              </el-tag>
              <el-tag :type="day.error_tags.length > 0 ? 'warning' : 'success'" size="small" effect="plain">
                {{ day.error_tags.length > 0 ? '有错误' : '无错误' }}
              </el-tag>
            </div>
          </div>

          <div class="day-body">
            <div v-if="day.llm_interactions.length > 0" class="section-block">
              <div class="section-title">LLM 建议</div>
              <div v-for="llm in day.llm_interactions" :key="llm.id" class="item-row">
                <el-tag :type="llm.applied ? 'success' : 'info'" size="small">{{ llm.applied ? '已采纳' : '未采纳' }}</el-tag>
                <span>{{ llm.order_action }} {{ llm.symbol }}</span>
                <span class="muted">{{ formatTime(llm.created_at) }}</span>
              </div>
            </div>

            <div v-if="day.orders.length > 0" class="section-block">
              <div class="section-title">订单执行</div>
              <div v-for="order in day.orders" :key="order.id" class="item-row" data-testid="review-order-detail">
                <el-tag :type="order.side === 'BUY' || order.side === 'BUY_TO_COVER' ? 'success' : 'danger'" size="small">{{ order.side }}</el-tag>
                <span>{{ order.broker_order_id }}</span>
                <span>{{ formatOrderIntent(order.quantity, order.price, diagnosticsMarket || 'US') }}</span>
                <span>{{ formatFilledQuantity(order.executed_quantity) }}</span>
                <span>{{ formatExecutedPrice(order.executed_price, diagnosticsMarket || 'US') }}</span>
                <el-tag :type="order.status === ORDER_STATUS.FILLED ? 'success' : 'warning'" size="small">{{ order.status }}</el-tag>
                <span class="muted">成交时间 {{ formatTime(order.filled_at) }}</span>
                <span class="muted">{{ formatTime(order.created_at) }}</span>
              </div>
            </div>

            <div v-if="day.events.length > 0" class="section-block">
              <div class="section-title">交易事件</div>
              <div v-for="event in day.events" :key="event.id" class="item-row" data-testid="review-event-payload">
                <el-tag :type="eventType(event.event_type)" size="small">{{ event.event_type }}</el-tag>
                <span>{{ event.message || '-' }}</span>
                <span>{{ payloadPreview(event.payload_json) }}</span>
                <span class="muted">{{ formatTime(event.created_at) }}</span>
              </div>
            </div>

            <div v-if="day.snapshots.length > 0" class="section-block">
              <div class="section-title">行情快照</div>
              <div v-for="snap in day.snapshots" :key="snap.id" class="item-row" data-testid="review-snapshot-detail">
                <span>价格 {{ formatCurrency(snap.last_price, diagnosticsMarket) }}</span>
                <span>{{ triggerDeltaText(snap.last_price, snap.last_trigger_price, diagnosticsMarket || 'US') }}</span>
                <span>连亏 {{ snap.consecutive_losses }}</span>
                <span v-if="snap.daily_pnl !== 0" :class="snap.daily_pnl >= 0 ? 'positive' : 'negative'">{{ signedCurrency(snap.daily_pnl) }}</span>
                <span class="muted">{{ formatTime(snap.created_at) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <el-empty v-else-if="searched && !loading" description="该时间段无数据，请调整筛选条件" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import PriceChart from '../components/PriceChart.vue'
import PnLChart from '../components/PnLChart.vue'
import RiskHistoryPanel from '../components/RiskHistoryPanel.vue'
import { getReview, exportReview } from '../api/review'
import { getLLMIntervalStatus } from '../api'
import { useStatusHistorySeries } from '../composables/useStatusHistorySeries'
import { useDiagnosticsSnapshot } from '../composables/useDiagnosticsSnapshot'
import type { LLMIntervalStatus, ReviewResponse } from '../types'
import { ORDER_STATUS } from '../utils/constants'
import { downloadCsv } from '../utils/csv'
import { formatCurrency, marketFromSymbol } from '../utils/format'

const form = ref({
  symbol: 'AAPL.US',
  from_date: '',
  to_date: '',
})

type ReviewDayFilter = 'all' | 'losing' | 'winning' | 'error' | 'traded' | 'no_trade' | 'llm' | 'event'

const loading = ref(false)
const searched = ref(false)
const reviewData = ref<ReviewResponse | null>(null)
const dayFilter = ref<ReviewDayFilter>('all')
const timelineKeyword = ref('')
const compactReviewMode = ref(false)
const REVIEW_WORKBENCH_PREFS_KEY = 'auto_trade.review.workbench.v1'
const VALID_REVIEW_DAY_FILTERS: ReviewDayFilter[] = ['all', 'losing', 'winning', 'error', 'traded', 'no_trade', 'llm', 'event']
const {
  history: runtimeHistory,
  loading: runtimeHistoryLoading,
  error: runtimeHistoryError,
  load: loadRuntimeHistory,
  reset: resetRuntimeHistory,
} = useStatusHistorySeries()
const {
  diagnostics,
  error: diagnosticsError,
  selectedRuntime: selectedRuntimeDiagnostics,
  load: loadDiagnostics,
  reset: resetDiagnostics,
} = useDiagnosticsSnapshot(computed(() => form.value.symbol))

const pnlClass = computed(() => {
  if (!reviewData.value) return ''
  return reviewData.value.total_pnl >= 0 ? 'positive' : 'negative'
})

const eventSearchText = (day: ReviewResponse['days'][number]) =>
  [
    day.date,
    day.symbol,
    day.daily_pnl,
    day.trade_count,
    ...day.error_tags,
    ...day.llm_interactions.map((llm) => `${llm.order_action} ${llm.symbol} ${llm.order_status} ${llm.interaction_type}`),
    ...day.events.map((event) => `${event.event_type} ${event.message ?? ''} ${event.payload_json ?? ''}`),
    ...day.orders.map(
      (order) =>
        `${order.side} ${order.status} ${order.broker_order_id} ${order.quantity} ${order.price} ${order.executed_quantity ?? ''} ${order.executed_price ?? ''} ${order.created_at ?? ''} ${order.filled_at ?? ''}`,
    ),
    ...day.snapshots.map(
      (snapshot) =>
        `${snapshot.engine_state} ${snapshot.daily_pnl} ${snapshot.consecutive_losses} ${snapshot.last_price ?? ''} ${snapshot.last_trigger_price ?? ''} ${snapshot.created_at ?? ''}`,
    ),
  ]
    .join(' ')
    .toLowerCase()

const dayMatchesQuickFilter = (day: ReviewResponse['days'][number], filter: ReviewDayFilter) => {
  if (filter === 'all') return true
  if (filter === 'losing') return day.daily_pnl < 0
  if (filter === 'winning') return day.daily_pnl > 0
  if (filter === 'error') return day.error_tags.length > 0
  if (filter === 'traded') return day.trade_count > 0
  if (filter === 'no_trade') return day.trade_count === 0
  if (filter === 'llm') return day.llm_interactions.length > 0
  if (filter === 'event') return day.events.length > 0
  return true
}

const filteredReviewDays = computed(() => {
  if (!reviewData.value) return []
  const keyword = timelineKeyword.value.trim().toLowerCase()
  return reviewData.value.days.filter((day) => dayMatchesQuickFilter(day, dayFilter.value) && (keyword === '' || eventSearchText(day).includes(keyword)))
})

const visibleSnapshots = computed(() => filteredReviewDays.value.flatMap((day) => day.snapshots))

const visibleOrders = computed(() => filteredReviewDays.value.flatMap((day) => day.orders))

const visibleLlmInteractions = computed(() => filteredReviewDays.value.flatMap((day) => day.llm_interactions))

const visibleEvents = computed(() => filteredReviewDays.value.flatMap((day) => day.events))

type ReviewTimelineRow = {
  date: string
  source: string
  symbol: string
  type: string
  status: string
  side: string
  message: string
  pnl: string
  created_at: string
  broker_order_id: string
}

const visibleTimelineRows = computed<ReviewTimelineRow[]>(() => {
  const rows: ReviewTimelineRow[] = []

  for (const day of filteredReviewDays.value) {
    for (const llm of day.llm_interactions) {
      rows.push({
        date: day.date,
        source: 'llm',
        symbol: llm.symbol ?? day.symbol ?? '',
        type: llm.interaction_type ?? '',
        status: llm.order_status ?? '',
        side: llm.order_action ?? '',
        message: llm.success ? 'success' : 'failed',
        pnl: '',
        created_at: llm.created_at ?? '',
        broker_order_id: llm.order_id ?? '',
      })
    }

    for (const order of day.orders) {
      rows.push({
        date: day.date,
        source: 'order',
        symbol: order.symbol ?? day.symbol ?? '',
        type: 'order',
        status: order.status ?? '',
        side: order.side ?? '',
        message: `${formatOrderIntent(order.quantity, order.price, diagnosticsMarket.value || 'US')} ${formatFilledQuantity(order.executed_quantity)} ${formatExecutedPrice(order.executed_price, diagnosticsMarket.value || 'US')}`.trim(),
        pnl: '',
        created_at: order.created_at ?? '',
        broker_order_id: order.broker_order_id ?? '',
      })
    }

    for (const event of day.events) {
      rows.push({
        date: day.date,
        source: 'event',
        symbol: event.symbol ?? day.symbol ?? '',
        type: event.event_type ?? '',
        status: event.status ?? '',
        side: event.side ?? '',
        message: [event.message, payloadExportText(event.payload_json)].filter((value) => value !== '').join(' '),
        pnl: '',
        created_at: event.created_at ?? '',
        broker_order_id: event.broker_order_id ?? '',
      })
    }

    for (const snapshot of day.snapshots) {
      rows.push({
        date: day.date,
        source: 'snapshot',
        symbol: day.symbol ?? '',
        type: snapshot.engine_state ?? '',
        status: snapshot.daily_pnl >= 0 ? 'positive' : 'negative',
        side: '',
        message: `last_price=${snapshot.last_price ?? ''} trigger=${snapshot.last_trigger_price ?? ''} losses=${snapshot.consecutive_losses ?? ''}`,
        pnl: signedCurrency(snapshot.daily_pnl ?? 0),
        created_at: snapshot.created_at ?? '',
        broker_order_id: '',
      })
    }
  }

  return rows.sort(compareTimelineRows)
})

function compareTimelineRows(a: ReviewTimelineRow, b: ReviewTimelineRow): number {
  const dateOrder = compareStrings(a.date, b.date)
  if (dateOrder !== 0) return dateOrder

  const createdAtOrder = compareStrings(a.created_at, b.created_at)
  if (createdAtOrder !== 0) return createdAtOrder

  const sourceOrder = compareStrings(a.source, b.source)
  if (sourceOrder !== 0) return sourceOrder

  const typeOrder = compareStrings(a.type, b.type)
  if (typeOrder !== 0) return typeOrder

  const statusOrder = compareStrings(a.status, b.status)
  if (statusOrder !== 0) return statusOrder

  const brokerOrderIdOrder = compareStrings(a.broker_order_id, b.broker_order_id)
  if (brokerOrderIdOrder !== 0) return brokerOrderIdOrder

  return compareStrings(a.symbol, b.symbol)
}

function compareStrings(a: string, b: string): number {
  return a.localeCompare(b, 'zh-Hans-CN-u-co-compat', { numeric: true, sensitivity: 'base' })
}

const snapshotSummary = computed(() => {
  const snapshots = visibleSnapshots.value

  if (snapshots.length === 0) {
    return {
      count: 0,
      minPrice: null as string | null,
      maxPrice: null as string | null,
      latestState: '',
      maxConsecutiveLosses: null as string | null,
      maxTriggerDistance: null as string | null,
    }
  }

  let minPrice: number | null = null
  let maxPrice: number | null = null
  let latestSnapshot = snapshots[0]
  let latestSnapshotTime = parseSnapshotTime(snapshots[0].created_at)
  let latestSnapshotIndex = 0
  let maxConsecutiveLosses: number | null = null
  let maxTriggerDistance: number | null = null

  snapshots.forEach((snapshot, index) => {
    const lastPrice = toFiniteNumber(snapshot.last_price)
    const triggerPrice = toFiniteNumber(snapshot.last_trigger_price)

    if (lastPrice !== null) {
      minPrice = minPrice === null ? lastPrice : Math.min(minPrice, lastPrice)
      maxPrice = maxPrice === null ? lastPrice : Math.max(maxPrice, lastPrice)
    }

    const snapshotTime = parseSnapshotTime(snapshot.created_at)
    if (snapshotTime > latestSnapshotTime || (snapshotTime === latestSnapshotTime && index > latestSnapshotIndex)) {
      latestSnapshot = snapshot
      latestSnapshotTime = snapshotTime
      latestSnapshotIndex = index
    }

    const consecutiveLosses = toFiniteNumber(snapshot.consecutive_losses)
    if (consecutiveLosses !== null) {
      maxConsecutiveLosses = maxConsecutiveLosses === null ? consecutiveLosses : Math.max(maxConsecutiveLosses, consecutiveLosses)
    }
    if (lastPrice !== null && triggerPrice !== null && triggerPrice > 0) {
      const triggerDistance = Math.abs(lastPrice - triggerPrice)
      maxTriggerDistance = maxTriggerDistance === null ? triggerDistance : Math.max(maxTriggerDistance, triggerDistance)
    }
  })

  return {
    count: snapshots.length,
    minPrice: formatSnapshotNumber(minPrice),
    maxPrice: formatSnapshotNumber(maxPrice),
    latestState: latestSnapshot.engine_state,
    maxConsecutiveLosses: formatSnapshotNumber(maxConsecutiveLosses),
    maxTriggerDistance: maxTriggerDistance === null ? null : formatSnapshotNumber(maxTriggerDistance, 2),
  }
})

const LLM_ACTIONS = ['BUY', 'SELL', 'SHORT', 'COVER', 'NONE', 'other'] as const
type LlmAction = (typeof LLM_ACTIONS)[number]

function normalizeLlmAction(action: string | null | undefined): LlmAction {
  const normalized = (action ?? '').trim().toUpperCase()
  if (normalized === '' || normalized === 'NONE') return 'NONE'
  if (LLM_ACTIONS.includes(normalized as LlmAction)) return normalized as Exclude<LlmAction, 'other'>
  return 'other'
}

const llmActionSummary = computed(() => {
  const actionCounts = LLM_ACTIONS.reduce<Record<LlmAction, number>>((counts, action) => {
    counts[action] = 0
    return counts
  }, {} as Record<LlmAction, number>)

  let success = 0
  let applied = 0
  let linked = 0

  for (const llm of visibleLlmInteractions.value) {
    const action = normalizeLlmAction(llm.order_action)
    actionCounts[action] += 1
    if (llm.success) success += 1
    if (llm.applied) applied += 1
    if (Boolean(llm.order_id)) linked += 1
  }

  return {
    actions: LLM_ACTIONS,
    actionCounts,
    total: visibleLlmInteractions.value.length,
    success,
    applied,
    linked,
  }
})

const EVENT_BUCKETS = ['risk', 'order', 'session', 'broker', 'llm', 'other'] as const
type EventBucket = (typeof EVENT_BUCKETS)[number]

const EVENT_BUCKET_LABELS: Record<EventBucket, string> = {
  risk: '风险',
  order: '订单',
  session: '时段',
  broker: '券商',
  llm: 'LLM',
  other: '其他',
}

const EVENT_BUCKET_PATTERNS: Record<Exclude<EventBucket, 'other'>, RegExp> = {
  risk: /RISK|LOSS|KILL|PAUSE/,
  order: /ORDER|FILLED|REJECT|CANCEL|TIMEOUT/,
  session: /SESSION|RTH|MARKET/,
  broker: /BROKER|QUOTE|LONGPORT|STREAM/,
  llm: /LLM|ADVISOR|INTERVAL/,
}

function eventBucketText(value: string | null | undefined): string {
  return (value ?? '').toUpperCase()
}

function classifyEventBucket(event: ReviewResponse['days'][number]['events'][number]): EventBucket {
  const text = [event.event_type, event.status, event.message, event.payload_json].map(eventBucketText).join(' ')
  if (EVENT_BUCKET_PATTERNS.risk.test(text)) return 'risk'
  if (EVENT_BUCKET_PATTERNS.order.test(text)) return 'order'
  if (EVENT_BUCKET_PATTERNS.session.test(text)) return 'session'
  if (EVENT_BUCKET_PATTERNS.broker.test(text)) return 'broker'
  if (EVENT_BUCKET_PATTERNS.llm.test(text)) return 'llm'
  return 'other'
}

const eventBucketSummary = computed(() => {
  const counts: Record<EventBucket, number> = {
    risk: 0,
    order: 0,
    session: 0,
    broker: 0,
    llm: 0,
    other: 0,
  }

  for (const event of visibleEvents.value) {
    counts[classifyEventBucket(event)] += 1
  }

  return { total: visibleEvents.value.length, ...counts }
})

const EXECUTION_FAILED_STATUSES = ['REJECT', 'FAIL', 'CANCEL', 'TIMEOUT', 'ERROR'] as const

const executionQuality = computed(() => {
  let filled = 0
  let partial = 0
  let open = 0
  let failed = 0
  let slippageTotal = 0
  let slippageSamples = 0

  for (const order of visibleOrders.value) {
    const status = statusText(order.status)
    const executedQuantity = order.executed_quantity ?? 0
    const quantity = order.quantity ?? 0
    const isFailed = EXECUTION_FAILED_STATUSES.some((failedStatus) => status.includes(failedStatus))
    const isPartial = status.includes('PARTIAL') || (executedQuantity > 0 && executedQuantity < quantity)
    const isFilled = status === 'FILLED' || executedQuantity >= quantity
    const isOpen = /SUBMIT|NEW|PENDING|OPEN/.test(status) || status === ''

    if (isPartial) {
      partial += 1
    } else if (isFailed) {
      failed += 1
    } else if (isFilled) {
      filled += 1
    } else if (isOpen) {
      open += 1
    } else {
      open += 1
    }

    if (typeof order.executed_price === 'number' && Number.isFinite(order.executed_price) && typeof order.price === 'number' && Number.isFinite(order.price)) {
      slippageTotal += order.executed_price - order.price
      slippageSamples += 1
    }
  }

  const avgSlippage = slippageSamples > 0 ? `${(slippageTotal / slippageSamples).toFixed(2)}` : '-'

  return { filled, partial, open, failed, slippageSamples, avgSlippage }
})

const resetWorkbenchFilters = () => {
  dayFilter.value = 'all'
  timelineKeyword.value = ''
}

function isReviewDayFilter(value: unknown): value is ReviewDayFilter {
  return typeof value === 'string' && VALID_REVIEW_DAY_FILTERS.includes(value as ReviewDayFilter)
}

function loadWorkbenchPrefs() {
  try {
    const raw = localStorage.getItem(REVIEW_WORKBENCH_PREFS_KEY)
    if (!raw) return

    const parsed: unknown = JSON.parse(raw)
    if (parsed == null || typeof parsed !== 'object') return

    const prefs = parsed as { dayFilter?: unknown; timelineKeyword?: unknown; compactReviewMode?: unknown }
    if (isReviewDayFilter(prefs.dayFilter)) {
      dayFilter.value = prefs.dayFilter
    }
    if (typeof prefs.timelineKeyword === 'string') {
      timelineKeyword.value = prefs.timelineKeyword
    }
    if (typeof prefs.compactReviewMode === 'boolean') {
      compactReviewMode.value = prefs.compactReviewMode
    }
  } catch {
    // Weak dependency: localStorage is best-effort client state only; invalid JSON or browser storage errors must not block the page.
  }
}

watch([dayFilter, timelineKeyword, compactReviewMode], () => {
  try {
    localStorage.setItem(
      REVIEW_WORKBENCH_PREFS_KEY,
      JSON.stringify({
        dayFilter: dayFilter.value,
        timelineKeyword: timelineKeyword.value,
        compactReviewMode: compactReviewMode.value,
      }),
    )
  } catch {
    // Weak dependency: persistence is optional and should never prevent review workbench interaction.
  }
})

type ReviewHealth = {
  score: number
  label: string
  tagType: 'success' | 'warning' | 'danger'
  reasons: string[]
}

const reviewHealth = computed<ReviewHealth>(() => {
  if (!reviewData.value || reviewData.value.days.length === 0) {
    return { score: 100, label: '健康', tagType: 'success', reasons: [] }
  }

  let score = 100
  const reasons: string[] = []
  const hasProfitLoss = reviewData.value.total_pnl < 0
  const errorTags = reviewData.value.all_error_tags.length
  let hasOrderIssue = false
  let failedLlmCount = 0
  let maxConsecutiveLosses = 0
  for (const day of reviewData.value.days) {
    if (!hasOrderIssue && day.orders.some((order) => /REJECT|FAIL|CANCEL|TIMEOUT/.test(statusText(order.status)))) {
      hasOrderIssue = true
    }
    failedLlmCount += day.llm_interactions.filter((llm) => !llm.success).length
    for (const snapshot of day.snapshots) {
      maxConsecutiveLosses = Math.max(maxConsecutiveLosses, snapshot.consecutive_losses)
    }
  }

  if (hasProfitLoss) {
    score -= 25
    reasons.push('区间亏损')
  }
  if (errorTags > 0) {
    score -= Math.min(25, errorTags * 10)
    reasons.push('存在错误')
  }

  if (hasOrderIssue) {
    score -= 15
    reasons.push('订单异常')
  }
  if (failedLlmCount > 0) {
    score -= Math.min(failedLlmCount * 5, 15)
    reasons.push('LLM 失败')
  }
  if (maxConsecutiveLosses >= 2) {
    score -= 10
    reasons.push('连亏压力')
  }

  score = Math.max(0, Math.min(100, score))

  const label = score >= 80 ? '健康' : score >= 50 ? '需关注' : '高风险'
  const tagType = score >= 80 ? 'success' : score >= 50 ? 'warning' : 'danger'

  return { score, label, tagType, reasons: reasons.slice(0, 3) }
})

const reviewBriefText = computed(() => {
  if (!reviewData.value || reviewData.value.days.length === 0) return ''

  const health = reviewHealth.value
  const errorTags = reviewData.value.all_error_tags.length > 0 ? reviewData.value.all_error_tags.join('、') : '无'
  const llmActions = llmActionSummary.value.actions
    .filter((action) => llmActionSummary.value.actionCounts[action] > 0)
    .map((action) => `${action} ${llmActionSummary.value.actionCounts[action]}`)
    .join('，') || '无'
  const orderQuality = `成交 ${executionQuality.value.filled}，部分 ${executionQuality.value.partial}，挂起 ${executionQuality.value.open}，异常 ${executionQuality.value.failed}，平均滑点 ${executionQuality.value.avgSlippage}`

  return [
    `摘要：${reviewData.value.symbol} · ${reviewData.value.from_date} ~ ${reviewData.value.to_date}`,
    `复盘健康：${health.label} ${health.score}`,
    `总盈亏：${signedCurrency(reviewData.value.total_pnl)} · 交易：${reviewData.value.total_trades} · 当前筛选天数：${filteredReviewDays.value.length}`,
    `错误标签：${errorTags}`,
    `LLM 动作：${llmActions}（成功 ${llmActionSummary.value.success}/${llmActionSummary.value.total}，已应用 ${llmActionSummary.value.applied}）`,
    `订单质量：${orderQuality}`,
  ].join('\n')
})

function statusText(value: string | null | undefined): string {
  return (value ?? '').toUpperCase()
}

function exportVisibleTimelineCsv() {
  if (visibleTimelineRows.value.length === 0) {
    ElMessage.warning('当前筛选结果无可导出的复盘行')
    return
  }

  downloadCsv(
    `review_visible_timeline.csv`,
    [
      { key: 'date', label: '日期' },
      { key: 'source', label: '来源' },
      { key: 'symbol', label: '标的' },
      { key: 'type', label: '类型' },
      { key: 'status', label: '状态' },
      { key: 'side', label: '方向' },
      { key: 'message', label: '消息' },
      { key: 'pnl', label: '盈亏' },
      { key: 'created_at', label: '时间' },
      { key: 'broker_order_id', label: '订单号' },
    ],
    visibleTimelineRows.value,
  )
  ElMessage.success('已导出当前筛选复盘 CSV')
}

async function copyReviewBrief() {
  if (!reviewBriefText.value) return
  try {
    await navigator.clipboard.writeText(reviewBriefText.value)
    ElMessage.success('复盘摘要已复制')
  } catch {
    ElMessage.error('复制失败，请检查浏览器剪贴板权限')
  }
}

const reviewSymbolLabel = computed(() => {
  if (runtimeHistory.value.points.length === 0) return form.value.symbol || '未选择标的'
  return `${form.value.symbol} · ${runtimeHistory.value.points.length} 个样本`
})

const diagnosticsSymbolLabel = computed(() => {
  if (selectedRuntimeDiagnostics.value) {
    return `${selectedRuntimeDiagnostics.value.symbol} · ${selectedRuntimeDiagnostics.value.engine_state}`
  }
  return form.value.symbol || '未选择标的'
})

const diagnosticsMarket = computed(() => marketFromSymbol(form.value.symbol))

const llmStatus = ref<LLMIntervalStatus | null>(null)
const llmStatusLoading = ref(false)
const selectedSymbolLLMStatus = computed(() => {
  if (!llmStatus.value) return null
  return llmStatus.value.symbol_statuses.find((s) => s.symbol === form.value.symbol) || null
})

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

onMounted(() => {
  loadWorkbenchPrefs()
  loadLLMStatus()
})

async function handleSearch() {
  if (!form.value.symbol || !form.value.from_date || !form.value.to_date) {
    ElMessage.warning('请填写完整的查询条件')
    return
  }
  loading.value = true
  searched.value = true

  try {
    const [reviewResult, historyResult, diagnosticsResult] = await Promise.allSettled([
      getReview({
        symbol: form.value.symbol,
        from_date: form.value.from_date,
        to_date: form.value.to_date,
      }),
      loadRuntimeHistory({
        symbol: form.value.symbol,
        from: `${form.value.from_date}T00:00:00Z`,
        to: `${form.value.to_date}T23:59:59Z`,
        limit: 200,
      }),
      loadDiagnostics(),
    ])

    if (reviewResult.status === 'fulfilled') {
      reviewData.value = reviewResult.value
    } else {
      reviewData.value = null
      ElMessage.error('复盘数据加载失败')
    }

    if (historyResult.status === 'fulfilled') {
      runtimeHistory.value = historyResult.value
    } else {
      resetRuntimeHistory()
    }

    if (diagnosticsResult.status === 'fulfilled') {
      diagnostics.value = diagnosticsResult.value
    } else {
      resetDiagnostics()
    }
  } catch {
    ElMessage.error('查询复盘数据失败')
    reviewData.value = null
    resetRuntimeHistory()
    resetDiagnostics()
  } finally {
    loading.value = false
  }
}

function handleExport(fmt: 'json' | 'csv') {
  if (!form.value.symbol || !form.value.from_date || !form.value.to_date) return
  exportReview({
    symbol: form.value.symbol,
    from_date: form.value.from_date,
    to_date: form.value.to_date,
    format: fmt,
  })
    .then((res) => {
      const blob: Blob = res instanceof Blob ? res : new Blob([JSON.stringify(res)], { type: fmt === 'json' ? 'application/json' : 'text/csv' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `review_${form.value.symbol.split('.').join('_')}_${form.value.from_date}_${form.value.to_date}.${fmt}`
      document.body.appendChild(link)
      // Revoke the object URL as soon as the browser has had a chance to
      // process the click — listening for `click` is more deterministic than
      // setTimeout and avoids races when the user fires several exports in
      // quick succession (each link was previously holding its URL alive for
      // a full second, which leaks memory and can collide with later URLs).
      const cleanup = () => {
        URL.revokeObjectURL(url)
        link.removeEventListener('click', cleanup)
        if (link.parentNode) link.parentNode.removeChild(link)
      }
      link.addEventListener('click', cleanup)
      link.click()
      // Fallback: if the click event has already fired (synchronous dispatch
      // path), still schedule a short revoke to ensure the URL is freed.
      setTimeout(cleanup, 1000)
      ElMessage.success(`导出 ${fmt.toUpperCase()} 成功`)
    })
    .catch(() => {
      ElMessage.error('导出失败')
    })
}

function eventType(eventTypeValue: string): string {
  switch (eventTypeValue) {
    case 'ORDER_FILLED': return 'success'
    case 'ORDER_CANCELLED': return 'info'
    case 'ORDER_REJECTED': return 'danger'
    case 'ORDER_SKIPPED': return 'warning'
    case 'RISK_PAUSED': return 'danger'
    case 'LLM_ANALYSIS': return 'primary'
    default: return ''
  }
}

function signedCurrency(value: number): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '-'
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

function formatAgeSeconds(value: number | null | undefined): string {
  if (value == null) return '-'
  return `${value.toFixed(1)}s`
}

function formatDate(d: Date): string {
  return d.toISOString().slice(0, 10)
}

function setQuickRange(days: number) {
  const end = new Date()
  const start = new Date()
  start.setDate(end.getDate() - days + 1)
  form.value.to_date = formatDate(end)
  form.value.from_date = formatDate(start)
  handleSearch()
}

function formatFilledQuantity(value: number | null): string {
  return value != null ? `成交 ${value.toFixed(0)}` : '成交 -'
}

function formatOrderIntent(quantity: number, price: number, market: string): string {
  return `委托 ${quantity.toFixed(0)} 股 @ ${formatCurrency(price, market)}`
}

function formatExecutedPrice(value: number | null, market: string): string {
  return value != null ? `成交价 ${formatCurrency(value, market)}` : '成交价 -'
}

function payloadPreview(payloadJson: string | null | undefined): string {
  if (!payloadJson?.trim()) return 'payload -'
  try {
    const parsed: unknown = JSON.parse(payloadJson)
    if (parsed == null) return 'payload -'
    if (typeof parsed === 'string') return `payload ${truncateText(parsed)}`
    if (typeof parsed === 'number' || typeof parsed === 'boolean') return `payload ${String(parsed)}`
    if (Array.isArray(parsed)) return `payload [${parsed.slice(0, 3).map((item) => stringifyPreviewValue(item)).join(', ')}${parsed.length > 3 ? ', …' : ''}]`
    if (typeof parsed === 'object') {
      const entries = Object.entries(parsed as Record<string, unknown>).slice(0, 3)
      const preview = entries.map(([key, value]) => `${key}: ${stringifyPreviewValue(value)}`).join(', ')
      return preview ? `payload {${preview}${Object.keys(parsed as Record<string, unknown>).length > 3 ? ', …' : ''}}` : 'payload -'
    }
    return 'payload -'
  } catch {
    return `payload ${truncateText(payloadJson.trim())}`
  }
}

function payloadExportText(payloadJson: string | null | undefined): string {
  if (!payloadJson?.trim()) return ''
  try {
    JSON.parse(payloadJson)
    return payloadPreview(payloadJson)
  } catch {
    return payloadJson.trim()
  }
}

function stringifyPreviewValue(value: unknown): string {
  if (value == null) return 'null'
  if (typeof value === 'string') return truncateText(value)
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return Array.isArray(value) ? '[…]' : '{…}'
}

function truncateText(value: string): string {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value
}

function triggerDeltaText(lastPrice: number, lastTriggerPrice: number, market: string): string {
  if (lastTriggerPrice <= 0) return 'Δ触发 -'
  const delta = lastPrice - lastTriggerPrice
  const sign = delta > 0 ? '+' : delta < 0 ? '-' : ''
  return `触发价 ${formatCurrency(lastTriggerPrice, market)} · Δ触发 ${sign}${Math.abs(delta).toFixed(2)}`
}

function toFiniteNumber(value: number | string | null | undefined): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (trimmed === '') return null
    const parsed = Number(trimmed)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function parseSnapshotTime(value: string | null | undefined): number {
  if (!value) return Number.NEGATIVE_INFINITY
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY
}

function formatSnapshotNumber(value: number | null, fractionDigits = 2): string | null {
  if (value === null) return null
  if (Number.isInteger(value)) return value.toFixed(0)
  const factor = 10 ** fractionDigits
  const truncated = Math.trunc(value * factor) / factor
  const fixed = truncated.toFixed(fractionDigits)
  return fixed.replace(/\.0+$/, '').replace(/(\.[0-9]*?)0+$/, '$1')
}
</script>

<style scoped>
.review-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: calc(100vh - 120px);
  padding: 16px;
  background: #fff;
}

.review-page.review-compact {
  gap: 10px;
  padding: 10px;
}

.review-page.review-compact .review-workbench-toolbar,
.review-page.review-compact .filter-card,
.review-page.review-compact .llm-status-card,
.review-page.review-compact .risk-history-card,
.review-page.review-compact .review-health-card,
.review-page.review-compact .runtime-history-card {
  margin-bottom: 4px;
}

.review-page.review-compact .day-card {
  padding: 10px;
}

.review-page.review-compact .section-block {
  padding: 8px;
}

.review-page.review-compact .item-row {
  padding: 4px 0;
  gap: 6px;
}

.review-header h3 {
  margin: 0;
}

.review-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.filter-card {
  margin-bottom: 0;
}

.summary-row {
  margin-bottom: 8px;
}

.summary-card {
  text-align: center;
}

.summary-value {
  color: #172033;
  font-size: 24px;
  font-weight: 800;
  line-height: 1.2;
}

.summary-value.positive {
  color: #14884f;
}

.summary-value.negative {
  color: #c43838;
}

.summary-value.error-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 4px;
  font-size: 13px;
  font-weight: 400;
}

.no-tag {
  color: #909399;
}

.summary-label {
  margin-top: 4px;
  color: #6b7280;
  font-size: 12px;
}

.llm-status-card {
  margin-bottom: 8px;
}

.llm-status-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.llm-status-header p {
  margin: 4px 0 0;
  color: #6b7280;
  font-size: 12px;
}

.llm-status-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 12px;
  color: #4b5563;
  font-size: 13px;
}

.llm-budget-bar {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 12px;
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

.selected-symbol-llm-status .section-title {
  margin-bottom: 8px;
}

.selected-symbol-llm-status .item-row {
  padding: 5px 0;
}

.skip-reason {
  color: #f59e0b;
  font-size: 12px;
}

.runtime-history-card {
  margin-bottom: 8px;
}

.review-health-card {
  margin-bottom: 8px;
}

.review-health-body {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  text-align: center;
}

.review-health-score-value {
  color: #172033;
  font-size: 32px;
  font-weight: 800;
  line-height: 1;
}

.review-health-score-label {
  color: #6b7280;
  font-size: 13px;
}

.review-health-reasons {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px;
}

.risk-history-card {
  margin-bottom: 8px;
}

.runtime-history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.runtime-history-header p {
  margin: 4px 0 0;
  color: #6b7280;
  font-size: 12px;
}

.chart-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}

.diagnostics-summary-grid .section-block {
  height: 100%;
}

.timeline-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.day-card {
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  padding: 14px;
  background: #f7f9fc;
}

.day-header {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid #e1e7f0;
}

.day-header strong {
  color: #172033;
  font-size: 15px;
}

.day-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-left: auto;
}

.day-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.day-meta {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 10px;
}

.day-meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  color: #4b5563;
  font-size: 12px;
}

.section-block {
  border-radius: 6px;
  padding: 10px;
  background: #fff;
}

.section-title {
  margin-bottom: 6px;
  color: #4b5563;
  font-size: 12px;
  font-weight: 700;
}

.item-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid #f1f5f9;
}

.item-row:last-child {
  border-bottom: none;
}

.muted {
  color: #909399;
  font-size: 12px;
}

.positive {
  color: #14884f;
}

.negative {
  color: #c43838;
}

@media (max-width: 520px) {
  .review-page {
    padding: 8px;
    gap: 12px;
  }

  .day-header {
    flex-direction: column;
    align-items: flex-start;
  }

  .day-tags {
    margin-left: 0;
  }
}
</style>
