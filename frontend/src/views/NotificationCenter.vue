<template>
  <div class="notif-page">
    <div class="notif-header">
      <el-badge :value="unreadCount" :max="99" :hidden="unreadCount === 0" data-testid="notif-unread-badge">
        <h3>通知中心</h3>
      </el-badge>
      <div class="notif-actions">
        <el-select v-model="severityFilter" placeholder="全部级别" clearable style="width: 140px" data-testid="notif-severity">
          <el-option label="INFO" value="INFO" />
          <el-option label="WARNING" value="WARNING" />
          <el-option label="CRITICAL" value="CRITICAL" />
        </el-select>
        <el-select v-model="successFilter" placeholder="全部结果" clearable style="width: 120px" data-testid="notif-success">
          <el-option label="成功" value="true" />
          <el-option label="失败" value="false" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          clearable
          style="width: 260px"
          data-testid="notif-date-range"
        />
        <el-input
          v-model="searchText"
          placeholder="搜索标题/内容/错误"
          clearable
          style="width: 220px"
          data-testid="notif-search"
          data-view-search="true"
        />
        <el-select v-model="symbolFilter" placeholder="当前页标的" clearable style="width: 140px" data-testid="notif-symbol-filter">
          <el-option v-for="symbol in symbolOptions" :key="symbol" :label="symbol" :value="symbol" />
        </el-select>
        <el-select v-model="sourceFilter" placeholder="推断类别" clearable style="width: 130px" data-testid="notif-category-filter">
          <el-option label="全部类别" value="" />
          <el-option v-for="source in sourceOptions" :key="source" :label="source" :value="source" />
        </el-select>
        <el-select v-model="pageSize" style="width: 110px" data-testid="notif-page-size" @change="handlePageSizeChange">
          <el-option label="10 / 页" :value="10" />
          <el-option label="20 / 页" :value="20" />
          <el-option label="50 / 页" :value="50" />
        </el-select>
        <el-select v-model="sortOrder" style="width: 120px" data-testid="notif-sort-order">
          <el-option label="当前页最新优先" value="newest" />
          <el-option label="当前页最早优先" value="oldest" />
        </el-select>
        <el-button-group>
          <el-button :type="quickFilter === 'all' ? 'primary' : ''" :aria-pressed="quickFilter === 'all'" data-testid="notif-filter-all" @click="setQuickFilter('all')">全部</el-button>
          <el-button :type="quickFilter === 'failed' ? 'primary' : ''" :aria-pressed="quickFilter === 'failed'" data-testid="notif-filter-failed" @click="setQuickFilter('failed')">失败</el-button>
          <el-button :type="quickFilter === 'critical' ? 'primary' : ''" :aria-pressed="quickFilter === 'critical'" data-testid="notif-filter-critical" @click="setQuickFilter('critical')">CRITICAL</el-button>
          <el-button :type="quickFilter === 'warning' ? 'primary' : ''" :aria-pressed="quickFilter === 'warning'" data-testid="notif-filter-warning" @click="setQuickFilter('warning')">WARNING</el-button>
          <el-button :type="quickFilter === 'info' ? 'primary' : ''" :aria-pressed="quickFilter === 'info'" data-testid="notif-filter-info" @click="setQuickFilter('info')">INFO</el-button>
        </el-button-group>
        <el-button :type="unreadOnly ? 'primary' : ''" data-testid="notif-unread-only" @click="unreadOnly = !unreadOnly">仅未读</el-button>
        <el-button-group>
          <el-button :type="groupMode === 'day' ? 'primary' : ''" data-testid="notif-group-day" @click="groupMode = 'day'">按日期</el-button>
          <el-button :type="groupMode === 'result' ? 'primary' : ''" data-testid="notif-group-result" @click="groupMode = 'result'">按结果</el-button>
        </el-button-group>
        <el-button data-testid="notif-quick-recent-days" @click="setQuickRange('recent-days')">最近日期范围</el-button>
        <el-button :loading="exportingCsv" data-testid="notif-export-csv" @click="handleExport('csv')">导出 CSV</el-button>
        <el-button :loading="exportingJson" data-testid="notif-export-json" @click="handleExport('json')">导出 JSON</el-button>
        <el-button data-testid="notif-copy-page" @click="copyCurrentPage">复制当前页</el-button>
        <el-button data-testid="notif-reset-filters" @click="resetFilters">重置筛选</el-button>
        <el-button-group>
          <el-button :type="viewMode === 'cards' ? 'primary' : ''" data-testid="notif-view-cards" @click="viewMode = 'cards'">卡片</el-button>
          <el-button :type="viewMode === 'table' ? 'primary' : ''" data-testid="notif-view-table" @click="viewMode = 'table'">表格</el-button>
          <el-button :type="viewMode === 'timeline' ? 'primary' : ''" data-testid="notif-view-timeline" @click="viewMode = 'timeline'">时间线</el-button>
        </el-button-group>
        <el-popover v-if="viewMode === 'table'" :width="160" trigger="click" placement="bottom">
          <template #reference>
            <el-button data-testid="notif-columns-toggle">列</el-button>
          </template>
          <div class="col-toggle" data-testid="notif-columns-menu">
            <el-checkbox v-model="colVisible.severity">级别</el-checkbox>
            <el-checkbox v-model="colVisible.success">结果</el-checkbox>
            <el-checkbox v-model="colVisible.error">错误</el-checkbox>
          </div>
        </el-popover>
        <el-button data-testid="notif-mark-all-read" @click="handleMarkAllRead">全部已读</el-button>
        <el-button :loading="loading" data-testid="notif-refresh" @click="load">刷新</el-button>
      </div>
    </div>

    <div class="notif-stats" data-testid="notif-stats">
      <div class="stats-header">
        <div class="stats-title">
          <h4>投递统计</h4>
          <span class="stats-scope" data-testid="notif-stats-scope">{{ statsScopeLabel }}</span>
        </div>
        <div class="stats-controls">
          <span data-testid="notif-stats-range">
            <el-date-picker
              v-model="statsRange"
              type="daterange"
              range-separator="至"
              start-placeholder="统计开始"
              end-placeholder="统计结束"
              value-format="YYYY-MM-DD"
              clearable
              size="small"
              style="width: 240px"
            />
          </span>
          <el-button size="small" :loading="statsLoading" data-testid="notif-stats-reload" @click="loadStats">查询统计</el-button>
        </div>
      </div>
      <div v-if="statsError" class="stats-state" data-testid="notif-stats-error">
        <el-alert type="error" :title="statsError" :closable="false" show-icon />
        <el-button size="small" style="margin-top: 8px" data-testid="notif-stats-retry" @click="loadStats">重试</el-button>
      </div>
      <div v-else-if="statsLoading && !stats" class="stats-state" data-testid="notif-stats-loading">加载中…</div>
      <div v-else-if="stats && stats.total === 0" class="stats-state" data-testid="notif-stats-empty">所选范围暂无通知记录</div>
      <div v-else-if="stats" class="stats-body" :class="{ 'stats-refreshing': statsLoading }">
        <div class="stats-block" data-testid="notif-stats-totals">
          <div class="stats-block-label">总量</div>
          <div class="stats-numbers">
            <span>发送 <strong>{{ stats.total }}</strong></span>
            <span>成功 <strong>{{ stats.success }}</strong></span>
            <span>失败 <strong>{{ stats.failed }}</strong></span>
            <span>成功率 <strong>{{ stats.success_rate }}%</strong></span>
          </div>
          <div class="dist-ratio">
            <div class="dist-ratio-success" :style="{ width: stats.success_rate + '%' }" />
            <div class="dist-ratio-failure" :style="{ width: (100 - stats.success_rate) + '%' }" />
          </div>
        </div>
        <div class="stats-block" data-testid="notif-stats-severity">
          <div class="stats-block-label">严重度分布（统计范围）</div>
          <template v-if="stats.by_severity.length">
            <div v-for="bucket in stats.by_severity" :key="bucket.key" class="stats-row">
              <el-tag size="small" :type="severityType(bucket.key)">{{ bucket.key }}</el-tag>
              <span>{{ bucket.total }} 条 · 成功 {{ bucket.success }} · 失败 {{ bucket.failed }}</span>
            </div>
          </template>
          <div v-else class="stats-none">无数据</div>
        </div>
        <div class="stats-block" data-testid="notif-stats-channels">
          <div class="stats-block-label">失败渠道归因</div>
          <template v-if="stats.failures_by_channel.length">
            <div v-for="channel in stats.failures_by_channel" :key="channel.key" class="stats-row">
              <span class="stats-channel-key">{{ channelLabel(channel.key) }}</span>
              <span>{{ channel.count }} 次</span>
            </div>
          </template>
          <div v-else class="stats-none">无失败记录</div>
          <div class="stats-note">仅统计失败记录；成功投递不记录渠道。</div>
        </div>
        <div v-if="stats.daily.length" class="stats-block stats-trend" data-testid="notif-stats-trend">
          <div class="stats-block-label">每日投递（UTC 日期）</div>
          <svg :viewBox="`0 0 ${trendViewWidth} ${trendViewHeight}`" preserveAspectRatio="none" class="stats-trend-svg" data-testid="notif-stats-trend-svg">
            <g v-for="bar in trendBars" :key="bar.date">
              <rect :x="bar.x" :y="bar.successY" :width="bar.width" :height="bar.successHeight" class="bar-success" />
              <rect :x="bar.x" :y="bar.failedY" :width="bar.width" :height="bar.failedHeight" class="bar-failed" />
            </g>
          </svg>
          <div class="stats-trend-legend">
            <span><i class="stats-dot stats-dot-success" />成功</span>
            <span><i class="stats-dot stats-dot-failed" />失败</span>
            <span class="stats-trend-range">{{ stats.daily[0]?.date }} ~ {{ stats.daily[stats.daily.length - 1]?.date }}</span>
          </div>
        </div>
      </div>
    </div>

    <div class="notif-summary" data-testid="notif-summary">
      <el-space wrap :size="8">
        <el-tag type="info">当前页 {{ displayedItems.length }}/{{ total }}</el-tag>
        <el-tag v-if="unreadCount > 0" type="danger" data-testid="notif-unread-count">未读 {{ unreadCount }}</el-tag>
        <el-tag type="success">成功 {{ summary.success }}</el-tag>
        <el-tag type="danger">失败 {{ summary.failure }}</el-tag>
        <el-tag type="success" data-testid="notif-success-rate">成功率 {{ successRatioPct }}%</el-tag>
        <el-tag type="danger" data-testid="notif-error-rate">失败率 {{ failureRatioPct }}%</el-tag>
        <el-tag type="danger">CRITICAL {{ summary.critical }}</el-tag>
        <el-tag type="warning">WARNING {{ summary.warning }}</el-tag>
        <el-tag type="info">INFO {{ summary.info }}</el-tag>
        <el-tag type="info" data-testid="notif-time-span">{{ timeSpanLabel }}</el-tag>
        <el-tag type="info" data-testid="notif-page-size-note">每页 {{ pageSize }} 条</el-tag>
      </el-space>
    </div>

    <div v-if="searchText.trim()" class="notif-highlight" data-testid="notif-highlight">
      搜索词：<mark data-testid="notif-highlight-match">{{ searchText.trim() }}</mark>
    </div>

    <div class="notif-active-filters" data-testid="notif-active-filters">
      <el-space wrap :size="6">
        <el-tag v-if="activeFilterLabels.length === 0" type="info">无筛选</el-tag>
        <el-tag v-for="label in activeFilterLabels" :key="label" type="warning">{{ label }}</el-tag>
      </el-space>
    </div>

    <el-alert
      v-if="successFilter === 'false'"
      title="当前仅查看失败通知，可打开详情复制错误或重试发送。"
      type="warning"
      show-icon
      :closable="false"
      data-testid="notif-failure-note"
    />

    <div v-if="displayedItems.length > 0" class="notif-distribution" data-testid="notif-distribution">
      <div class="dist-block">
        <div class="dist-label">严重度分布（当前页）</div>
        <div class="dist-bars">
          <div v-for="bar in severityBars" :key="bar.label" class="dist-bar-item">
            <div class="dist-bar-track">
              <div
                class="dist-bar-fill"
                :style="{ width: bar.pct + '%', background: bar.color }"
                :data-testid="`dist-bar-${bar.key}`"
              />
            </div>
            <span class="dist-bar-text">{{ bar.label }} {{ bar.count }}</span>
          </div>
        </div>
      </div>
      <div class="dist-block">
        <div class="dist-label">成功 / 失败</div>
        <div class="dist-ratio">
          <div class="dist-ratio-success" :style="{ width: successRatioPct + '%' }"><span v-if="summary.success">{{ summary.success }}</span></div>
          <div class="dist-ratio-failure" :style="{ width: (100 - successRatioPct) + '%' }"><span v-if="summary.failure">{{ summary.failure }}</span></div>
        </div>
        <small class="dist-ratio-note">成功率 {{ successRatioPct }}%</small>
      </div>
    </div>

    <DataState
      v-if="displayedItems.length === 0"
      class="notif-empty"
      empty
      empty-text="没有匹配的通知"
    />

    <div v-else-if="viewMode === 'cards' && groupMode === 'result'" class="notif-result-groups" data-testid="notif-result-groups">
      <div v-for="group in resultGroups" :key="group.label" class="day-group">
        <div class="day-group-title">{{ group.label }}</div>
        <el-space direction="vertical" fill :size="6">
          <div
            v-for="item in group.items"
            :key="item.id"
            :data-testid="`notif-card-${item.id}`"
            class="day-item-wrapper"
            @click="openDetail(item)"
          >
            <el-card shadow="never" class="day-item">
              <div class="day-item-main">
                <span class="day-item-time">{{ formatDateTime(item.created_at) }}</span>
                <span v-if="isItemUnread(item)" class="unread-dot" />
                <el-tag size="small" :type="severityType(item.severity)">{{ item.severity }}</el-tag>
                <el-tag size="small" :type="item.success ? 'success' : 'danger'">{{ item.success ? '成功' : '失败' }}</el-tag>
                <el-tag size="small" type="info">推断 {{ sourceLabel(item) }}</el-tag>
              </div>
              <div class="day-item-title"><template v-for="(part, idx) in highlightParts(item.title)" :key="idx"><mark v-if="part.hit" data-testid="notif-highlight-match">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></div>
              <div class="day-item-content"><template v-for="(part, idx) in highlightParts(item.content)" :key="idx"><mark v-if="part.hit" data-testid="notif-highlight-match">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></div>
              <div v-if="item.error" class="day-item-error">{{ item.error }}</div>
              <div v-if="!item.success" style="margin-top: 8px">
                <el-button
                  size="small"
                  text
                  :loading="retryingMap[item.id]"
                  data-testid="notif-retry-btn"
                  @click.stop="handleRetry(item)"
                >重试发送</el-button>
              </div>
            </el-card>
          </div>
        </el-space>
      </div>
    </div>

    <div v-else-if="viewMode === 'cards'" class="notif-day-groups" data-testid="notif-day-groups">
      <div v-for="group in dayGroups" :key="group.day" class="day-group">
        <div class="day-group-title">{{ group.day }}</div>
        <el-space direction="vertical" fill :size="6">
          <div
            v-for="item in group.items"
            :key="item.id"
            :data-testid="`notif-card-${item.id}`"
            class="day-item-wrapper"
            @click="openDetail(item)"
          >
            <el-card shadow="never" class="day-item">
              <div class="day-item-main">
                <span class="day-item-time">{{ formatDateTime(item.created_at) }}</span>
                <span v-if="isItemUnread(item)" class="unread-dot" />
                <el-tag size="small" :type="severityType(item.severity)">{{ item.severity }}</el-tag>
                <el-tag size="small" :type="item.success ? 'success' : 'danger'">{{ item.success ? '成功' : '失败' }}</el-tag>
                <el-tag size="small" type="info">推断 {{ sourceLabel(item) }}</el-tag>
              </div>
              <div class="day-item-title"><template v-for="(part, idx) in highlightParts(item.title)" :key="idx"><mark v-if="part.hit" data-testid="notif-highlight-match">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></div>
              <div class="day-item-content"><template v-for="(part, idx) in highlightParts(item.content)" :key="idx"><mark v-if="part.hit" data-testid="notif-highlight-match">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></div>
              <div v-if="item.error" class="day-item-error"><template v-for="(part, idx) in highlightParts(item.error)" :key="idx"><mark v-if="part.hit" data-testid="notif-highlight-match">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></div>
              <div v-if="!item.success" style="margin-top: 8px">
                <el-button
                  size="small"
                  text
                  :loading="retryingMap[item.id]"
                  data-testid="notif-retry-btn"
                  @click.stop="handleRetry(item)"
                >重试发送</el-button>
              </div>
            </el-card>
          </div>
        </el-space>
      </div>
    </div>

    <el-table v-else-if="viewMode === 'table'" :data="displayedItems" size="small" class="responsive-table" v-loading="loading" data-testid="notif-list" @row-click="openDetail">
      <el-table-column prop="created_at" label="时间" min-width="170">
        <template #default="{ row }">
          <span v-if="isItemUnread(row)" class="unread-dot" />
          {{ formatDateTime(row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column v-if="colVisible.severity" label="级别" min-width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="severityType(row.severity)">{{ row.severity }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column v-if="colVisible.success" label="结果" min-width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.success ? 'success' : 'danger'">{{ row.success ? '成功' : '失败' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="title" label="标题" min-width="140" show-overflow-tooltip />
      <el-table-column prop="content" label="内容" min-width="240" show-overflow-tooltip />
      <el-table-column v-if="colVisible.error" prop="error" label="错误" min-width="160" show-overflow-tooltip />
    </el-table>

    <div v-else-if="viewMode === 'timeline'" class="notif-timeline" data-testid="notif-timeline">
      <el-timeline>
        <el-timeline-item
          v-for="item in displayedItems"
          :key="item.id"
          :type="timelineType(item.severity)"
          :timestamp="formatDateTime(item.created_at)"
          :data-testid="`notif-timeline-item-${item.id}`"
        >
          <div class="timeline-title">
            <span v-if="isItemUnread(item)" class="unread-dot" />
            {{ item.title }}
          </div>
          <div class="timeline-content">{{ item.content }}</div>
          <el-tag size="small" :type="item.success ? 'success' : 'danger'">{{ item.success ? '成功' : '失败' }}</el-tag>
          <div v-if="item.error" class="timeline-error">{{ item.error }}</div>
        </el-timeline-item>
      </el-timeline>
    </div>

    <div class="notif-footer">
      <el-pagination
        background
        layout="total, prev, pager, next"
        :total="total"
        :current-page="page"
        :page-size="pageSize"
        @current-change="handlePage"
      />
    </div>

    <el-dialog
      v-model="detailDialog.visible"
      :title="detailDialog.item?.title || '通知详情'"
      width="520px"
      data-testid="notif-detail-dialog"
    >
      <template v-if="detailDialog.item">
        <div class="detail-meta">
          <el-tag size="small" :type="severityType(detailDialog.item.severity)">{{ detailDialog.item.severity }}</el-tag>
          <el-tag size="small" :type="detailDialog.item.success ? 'success' : 'danger'">{{ detailDialog.item.success ? '成功' : '失败' }}</el-tag>
          <el-tag size="small" type="info">推断 {{ sourceLabel(detailDialog.item) }}</el-tag>
          <span>{{ formatDateTime(detailDialog.item.created_at) }}</span>
        </div>
        <div class="detail-meta-extra" data-testid="notif-detail-meta-extra">
          #{{ detailDialog.item.id }} · {{ detailDialog.item.title }} · {{ detailDialog.item.severity }}
        </div>
        <h4 class="detail-title">{{ detailDialog.item.title }}</h4>
        <p class="detail-content">{{ detailDialog.item.content }}</p>
        <el-button size="small" text data-testid="notif-copy-title" @click="copyContent(detailDialog.item.title)">复制标题</el-button>
        <el-button size="small" text data-testid="notif-copy-content" @click="copyContent(detailDialog.item.content)">复制内容</el-button>
        <div class="detail-nav">
          <el-button size="small" data-testid="notif-detail-prev" :disabled="detailIndex <= 0" @click="moveDetail(-1)">上一条</el-button>
          <el-button size="small" data-testid="notif-detail-next" :disabled="detailIndex >= displayedItems.length - 1" @click="moveDetail(1)">下一条</el-button>
        </div>
        <div v-if="detailDialog.item.error" class="detail-error">
          <div class="detail-error-header">
            <span>错误信息</span>
            <el-button size="small" text data-testid="notif-copy-error" @click="copyError(detailDialog.item.error)">复制</el-button>
          </div>
          <pre>{{ detailDialog.item.error }}</pre>
        </div>
        <div v-if="!detailDialog.item.success" style="margin-top: 12px">
          <el-button
            type="primary"
            size="small"
            :loading="retryingMap[detailDialog.item.id]"
            data-testid="notif-retry-btn"
            @click="handleRetry(detailDialog.item)"
          >重试发送</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { exportNotifications, getNotificationStats, getNotifications, retryNotification } from '../api'
import { useNotificationBadge } from '../composables/useNotificationBadge'
import type { NotificationLogOut, NotificationStatsResponse } from '../types'
import { resolveErrorMessage } from '../utils/error'
import DataState from '../components/DataState.vue'
import { usePersistedColumns } from '../composables/usePersistedColumns'

const POLL_INTERVAL_MS = 10000
const { unreadCount, markAllRead, isItemUnread, refresh: refreshBadge } = useNotificationBadge()
let pollTimer: ReturnType<typeof setInterval> | null = null

const items = ref<NotificationLogOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const loading = ref(false)
const exportingCsv = ref(false)
const exportingJson = ref(false)
const severityFilter = ref<string>('')
const successFilter = ref<string>('')
const dateRange = ref<string[] | null>(null)
const searchText = ref('')
const symbolFilter = ref('')
const sourceFilter = ref('')
const unreadOnly = ref(false)
const quickFilter = ref<'all' | 'failed' | 'critical' | 'warning' | 'info'>('all')
let applyingQuickFilter = false
const viewMode = ref<'cards' | 'table' | 'timeline'>('cards')
const groupMode = ref<'day' | 'result'>('day')
// Toggleable table columns (defaults visible so existing behaviour/tests
// are unaffected; user can hide 级别/结果/错误 in the table view).
const { visible: colVisible } = usePersistedColumns('auto_trade.notif.columns', {
  severity: true,
  success: true,
  error: true,
})
const sortOrder = ref<'newest' | 'oldest'>('newest')
const quickRangeLabel = ref('')
const detailDialog = reactive({
  visible: false,
  item: null as NotificationLogOut | null,
})
let searchDebounceTimer: number | undefined
let loadRequestId = 0

const retryingMap = reactive<Record<number, boolean>>({})
const PREF_KEY = 'auto_trade_notification_center_prefs_v2'

// Server-side delivery statistics (GET /api/notifications/stats). Aggregates
// the whole notification log for the chosen window — explicitly NOT the
// currently loaded page. Loading/filtering stats is read-only: it never calls
// the retry endpoint and never sends a test notification.
const stats = ref<NotificationStatsResponse | null>(null)
const statsLoading = ref(false)
const statsError = ref('')
const statsRange = ref<string[] | null>(null)

const statsScopeLabel = computed(() => {
  if (!stats.value) return '服务端全量日志（非当前页）'
  const from = stats.value.from_date
  const to = stats.value.to_date
  const range = from || to ? `${from ?? '最早'} 至 ${to ?? '最新'}` : '全部时间'
  return `服务端全量日志（非当前页）· 范围：${range}`
})

function channelLabel(key: string): string {
  if (key === 'serverchan') return 'Server酱'
  if (key === 'webhook') return 'Webhook'
  if (key === 'telegram') return 'Telegram'
  if (key === 'unknown') return '未识别渠道'
  return key
}

const trendViewWidth = 560
const trendViewHeight = 64
const trendBars = computed(() => {
  const daily = stats.value?.daily ?? []
  if (!daily.length) return []
  const maxTotal = Math.max(1, ...daily.map((d) => d.total))
  const slot = trendViewWidth / daily.length
  const width = Math.min(24, slot * 0.6)
  return daily.map((d, i) => {
    const successHeight = (d.success / maxTotal) * trendViewHeight
    const failedHeight = (d.failed / maxTotal) * trendViewHeight
    return {
      date: d.date,
      x: i * slot + (slot - width) / 2,
      width,
      successY: trendViewHeight - successHeight,
      successHeight,
      failedY: trendViewHeight - successHeight - failedHeight,
      failedHeight,
    }
  })
})

async function loadStats() {
  statsLoading.value = true
  statsError.value = ''
  try {
    const params: { from_date?: string; to_date?: string } = {}
    if (statsRange.value?.[0]) params.from_date = statsRange.value[0]
    if (statsRange.value?.[1]) params.to_date = statsRange.value[1]
    stats.value = await getNotificationStats(params)
  } catch (e) {
    statsError.value = resolveErrorMessage(e, '加载投递统计失败')
    stats.value = null
  } finally {
    statsLoading.value = false
  }
}

function sourceLabel(item: NotificationLogOut): string {
  const text = `${item.title} ${item.content} ${item.error || ''}`.toLowerCase()
  if (text.includes('webhook')) return 'Webhook'
  if (text.includes('风控') || text.includes('risk') || text.includes('kill switch')) return 'Risk'
  if (text.includes('日报') || text.includes('report')) return 'Report'
  if (text.includes('价格') || text.includes('alert')) return 'Alert'
  return 'System'
}

function highlightParts(text: string): Array<{ text: string; hit: boolean }> {
  const query = searchText.value.trim()
  if (!query) return [{ text, hit: false }]
  const lowerText = text.toLowerCase()
  const lowerQuery = query.toLowerCase()
  const idx = lowerText.indexOf(lowerQuery)
  if (idx === -1) return [{ text, hit: false }]
  return [
    { text: text.slice(0, idx), hit: false },
    { text: text.slice(idx, idx + query.length), hit: true },
    { text: text.slice(idx + query.length), hit: false },
  ].filter((part) => part.text.length > 0)
}

function matchesSymbol(item: NotificationLogOut, symbol: string): boolean {
  const haystack = `${item.title} ${item.content} ${item.error || ''}`.toUpperCase()
  return haystack.includes(symbol.toUpperCase())
}

function extractSymbols(item: NotificationLogOut): string[] {
  const text = `${item.title} ${item.content} ${item.error || ''}`
  return Array.from(text.matchAll(/\b[A-Z0-9]{1,8}\.(?:US|HK|SH|SZ|SG)\b/g)).map((match) => match[0])
}

const symbolOptions = computed(() => {
  const symbols = new Set<string>()
  for (const item of items.value) {
    for (const symbol of extractSymbols(item)) symbols.add(symbol)
  }
  return Array.from(symbols).sort((a, b) => a.localeCompare(b))
})

const sourceOptions = computed(() => {
  const sources = new Set<string>()
  for (const item of items.value) sources.add(sourceLabel(item))
  return Array.from(sources).sort((a, b) => a.localeCompare(b))
})

const displayedItems = computed(() => {
  const filtered = items.value.filter((item) => {
    if (symbolFilter.value && !matchesSymbol(item, symbolFilter.value)) return false
    if (sourceFilter.value && sourceLabel(item) !== sourceFilter.value) return false
    if (unreadOnly.value && !isItemUnread(item)) return false
    return true
  })
  return filtered.sort((a, b) => {
    const delta = new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
    return sortOrder.value === 'newest' ? delta : -delta
  })
})

const summary = computed(() => {
  let success = 0
  let failure = 0
  let critical = 0
  let warning = 0
  let info = 0
  for (const item of displayedItems.value) {
    if (item.success) success += 1
    else failure += 1
    if (item.severity === 'CRITICAL') critical += 1
    else if (item.severity === 'WARNING') warning += 1
    else info += 1
  }
  return { success, failure, critical, warning, info }
})

/** Severity distribution bars for the currently loaded page. Pure client-side
 * derivation from `items` — no extra request. */
const severityBars = computed(() => {
  const s = summary.value
  const rows = [
    { key: 'critical', label: 'CRITICAL', count: s.critical, color: '#c43838' },
    { key: 'warning', label: 'WARNING', count: s.warning, color: '#e6a23c' },
    { key: 'info', label: 'INFO', count: s.info, color: '#909399' },
  ]
  const max = Math.max(1, ...rows.map((r) => r.count))
  return rows.map((r) => ({ ...r, pct: Math.round((r.count / max) * 100) }))
})

const successRatioPct = computed(() => {
  const total = summary.value.success + summary.value.failure
  if (total === 0) return 0
  return Math.round((summary.value.success / total) * 100)
})

const failureRatioPct = computed(() => {
  const total = summary.value.success + summary.value.failure
  if (total === 0) return 0
  return Math.round((summary.value.failure / total) * 100)
})

const dayGroups = computed(() => {
  const groups = new Map<string, NotificationLogOut[]>()
  for (const item of displayedItems.value) {
    const day = new Date(item.created_at).toLocaleDateString([], { year: 'numeric', month: '2-digit', day: '2-digit' })
    const list = groups.get(day)
    if (list) list.push(item)
    else groups.set(day, [item])
  }
  return Array.from(groups.entries()).map(([day, groupItems]) => ({ day, items: groupItems }))
})

const resultGroups = computed(() => {
  const successItems = displayedItems.value.filter((item) => item.success)
  const failedItems = displayedItems.value.filter((item) => !item.success)
  return [
    { label: `成功通知 ${successItems.length}`, items: successItems },
    { label: `失败通知 ${failedItems.length}`, items: failedItems },
  ].filter((group) => group.items.length > 0)
})

const detailIndex = computed(() => {
  if (!detailDialog.item) return -1
  return displayedItems.value.findIndex((item) => item.id === detailDialog.item?.id)
})

const timeSpanLabel = computed(() => {
  if (displayedItems.value.length === 0) return '时间范围 --'
  const times = displayedItems.value.map((item) => new Date(item.created_at).getTime())
  const start = new Date(Math.min(...times))
  const end = new Date(Math.max(...times))
  return `时间 ${formatShortDate(start)} - ${formatShortDate(end)}`
})

const activeFilterLabels = computed(() => {
  const labels: string[] = []
  if (severityFilter.value) labels.push(`级别 ${severityFilter.value}`)
  if (successFilter.value === 'true') labels.push('成功')
  if (successFilter.value === 'false') labels.push('失败')
  if (dateRange.value?.[0] || dateRange.value?.[1]) labels.push('日期范围')
  if (searchText.value.trim()) labels.push(`搜索 ${searchText.value.trim()}`)
  if (symbolFilter.value) labels.push(`当前页标的 ${symbolFilter.value}`)
  if (sourceFilter.value) labels.push(`推断类别 ${sourceFilter.value}`)
  if (unreadOnly.value) labels.push('仅未读')
  if (quickRangeLabel.value) labels.push(quickRangeLabel.value)
  return labels
})

function savePrefs() {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({ viewMode: viewMode.value, pageSize: pageSize.value, sortOrder: sortOrder.value }))
  } catch {
    // Preference persistence is non-critical; keep interaction working when storage is unavailable.
  }
}

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREF_KEY)
    if (!raw) return
    const prefs = JSON.parse(raw) as { viewMode?: string; pageSize?: number; sortOrder?: string }
    if (prefs.viewMode === 'cards' || prefs.viewMode === 'table' || prefs.viewMode === 'timeline') viewMode.value = prefs.viewMode
    if (prefs.pageSize === 10 || prefs.pageSize === 20 || prefs.pageSize === 50) pageSize.value = prefs.pageSize
    if (prefs.sortOrder === 'newest' || prefs.sortOrder === 'oldest') sortOrder.value = prefs.sortOrder
  } catch {
    localStorage.removeItem(PREF_KEY)
  }
}

async function load() {
  const requestId = ++loadRequestId
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (severityFilter.value) params.severity = severityFilter.value
    if (successFilter.value) params.success = successFilter.value === 'true'
    if (dateRange.value?.[0]) params.from_date = dateRange.value[0]
    if (dateRange.value?.[1]) params.to_date = dateRange.value[1]
    if (searchText.value.trim()) params.q = searchText.value.trim()

    const data = await getNotifications(params)
    if (requestId !== loadRequestId) return
    items.value = data.items
    total.value = data.total
  } catch (e) {
    if (requestId !== loadRequestId) return
    ElMessage.error(resolveErrorMessage(e, '加载通知失败'))
  } finally {
    if (requestId === loadRequestId) loading.value = false
  }
}

function handlePage(next: number) {
  page.value = next
  load()
}

function handlePageSizeChange() {
  page.value = 1
  load()
}

function resetFilters() {
  severityFilter.value = ''
  successFilter.value = ''
  dateRange.value = null
  searchText.value = ''
  symbolFilter.value = ''
  sourceFilter.value = ''
  unreadOnly.value = false
  quickRangeLabel.value = ''
  groupMode.value = 'day'
  quickFilter.value = 'all'
  page.value = 1
  window.clearTimeout(searchDebounceTimer)
  load()
}

function setQuickRange(range: 'recent-days') {
  const now = new Date()
  const from = new Date(now.getTime() - 24 * 60 * 60 * 1000)
  dateRange.value = [from.toISOString().slice(0, 10), now.toISOString().slice(0, 10)]
  quickRangeLabel.value = range === 'recent-days' ? '最近日期范围' : ''
  page.value = 1
}

function setQuickFilter(value: typeof quickFilter.value) {
  applyingQuickFilter = true
  quickFilter.value = value
  if (value === 'all') {
    severityFilter.value = ''
    successFilter.value = ''
  } else if (value === 'failed') {
    severityFilter.value = ''
    successFilter.value = 'false'
  } else {
    severityFilter.value = value.toUpperCase()
    successFilter.value = ''
  }
  window.clearTimeout(searchDebounceTimer)
  page.value = 1
  load()
  void nextTick(() => {
    applyingQuickFilter = false
  })
}

function debouncedLoad() {
  window.clearTimeout(searchDebounceTimer)
  searchDebounceTimer = window.setTimeout(() => {
    page.value = 1
    load()
  }, 300)
}

async function handleMarkAllRead() {
  await markAllRead()
  ElMessage.success('已全部标记为已读')
}

async function handleExport(format: 'csv' | 'json') {
  const loadingRef = format === 'csv' ? exportingCsv : exportingJson
  loadingRef.value = true
  try {
    const params: Record<string, unknown> = {}
    if (severityFilter.value) params.severity = severityFilter.value
    if (successFilter.value) params.success = successFilter.value === 'true'
    if (dateRange.value?.[0]) params.from_date = dateRange.value[0]
    if (dateRange.value?.[1]) params.to_date = dateRange.value[1]
    if (searchText.value.trim()) params.q = searchText.value.trim()

    const blob = await exportNotifications(format, params)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `notifications_${new Date().toISOString().slice(0, 10)}.${format}`
    document.body.appendChild(link)
    const cleanup = () => {
      URL.revokeObjectURL(url)
      link.removeEventListener('click', cleanup)
      if (link.parentNode) link.parentNode.removeChild(link)
    }
    link.addEventListener('click', cleanup)
    link.click()
    setTimeout(cleanup, 1000)
    ElMessage.success(`导出 ${format.toUpperCase()} 成功`)
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, `导出 ${format.toUpperCase()} 失败`))
  } finally {
    ;(format === 'csv' ? exportingCsv : exportingJson).value = false
  }
}

function openDetail(item: NotificationLogOut) {
  detailDialog.item = item
  detailDialog.visible = true
}

function moveDetail(delta: number) {
  const next = detailIndex.value + delta
  const item = displayedItems.value[next]
  if (item) detailDialog.item = item
}

async function copyCurrentPage() {
  const text = displayedItems.value.map((item) => {
    return `#${item.id} [${item.severity}] ${item.success ? '成功' : '失败'} ${item.title} — ${item.content}`
  }).join('\n')
  await copyContent(text)
}

async function copyError(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败')
  }
}

async function copyContent(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制通知内容')
  } catch {
    ElMessage.error('复制失败')
  }
}

async function handleRetry(item: NotificationLogOut) {
  if (retryingMap[item.id]) return
  retryingMap[item.id] = true
  try {
    const updated = await retryNotification(item.id)
    const idx = items.value.findIndex((i) => i.id === item.id)
    if (idx !== -1) {
      items.value[idx] = updated
    }
    if (detailDialog.item?.id === item.id) {
      detailDialog.item = updated
    }
    ElMessage.success(updated.success ? '重试成功' : '重试失败，请检查通知渠道配置')
    refreshBadge()
    // A completed retry changes the aggregate outcome; refresh the read-only
    // stats so the card does not keep showing a stale failure count.
    void loadStats()
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '重试失败'))
  } finally {
    retryingMap[item.id] = false
  }
}

function severityType(s: string): string {
  if (s === 'CRITICAL') return 'danger'
  if (s === 'WARNING') return 'warning'
  return 'info'
}

function timelineType(s: string): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  const t = severityType(s)
  if (t === 'danger') return 'danger'
  if (t === 'warning') return 'warning'
  return 'info'
}

function formatDateTime(v: string): string {
  return new Date(v).toLocaleString([], {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function formatShortDate(v: Date): string {
  return `${String(v.getMonth() + 1).padStart(2, '0')}/${String(v.getDate()).padStart(2, '0')}`
}

function startPoll() {
  stopPoll()
  pollTimer = setInterval(() => {
    load()
    refreshBadge()
  }, POLL_INTERVAL_MS)
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

watch(severityFilter, () => {
  if (applyingQuickFilter) return
  quickFilter.value = 'all'
  page.value = 1
  load()
})
watch(successFilter, () => {
  if (applyingQuickFilter) return
  quickFilter.value = 'all'
  page.value = 1
  load()
})
watch(dateRange, () => { page.value = 1; load() }, { deep: true })
watch(sourceFilter, () => { page.value = 1 })
watch(unreadOnly, () => { page.value = 1 })
watch(searchText, debouncedLoad)
watch([viewMode, pageSize, sortOrder], savePrefs)
onMounted(() => {
  loadPrefs()
  load()
  loadStats()
  startPoll()
})
onUnmounted(() => {
  window.clearTimeout(searchDebounceTimer)
  stopPoll()
})
</script>

<style scoped>
.col-toggle {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.notif-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  background: #fff;
  min-height: calc(100vh - 120px);
}

.notif-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.notif-header h3 {
  margin: 0;
}

.notif-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.notif-summary {
  padding: 8px 0;
}

.notif-stats {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px;
  background: #fafbfc;
}

.stats-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.stats-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.stats-title h4 {
  margin: 0;
  font-size: 14px;
}

.stats-scope {
  color: #909399;
  font-size: 12px;
}

.stats-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.stats-state {
  padding: 16px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}

.stats-body {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  transition: opacity 0.15s ease;
}

.stats-refreshing {
  opacity: 0.6;
}

.stats-block-label {
  margin-bottom: 8px;
  color: #606266;
  font-size: 13px;
  font-weight: 600;
}

.stats-numbers {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-bottom: 8px;
  color: #606266;
  font-size: 13px;
}

.stats-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  color: #606266;
  font-size: 12px;
}

.stats-channel-key {
  min-width: 72px;
  font-weight: 600;
}

.stats-none,
.stats-note {
  color: #909399;
  font-size: 12px;
}

.stats-trend {
  grid-column: 1 / -1;
}

.stats-trend-svg {
  display: block;
  width: 100%;
  height: 64px;
}

.stats-trend-svg .bar-success {
  fill: #14884f;
}

.stats-trend-svg .bar-failed {
  fill: #c43838;
}

.stats-trend-legend {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 6px;
  color: #909399;
  font-size: 12px;
}

.stats-trend-range {
  margin-left: auto;
}

.stats-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  margin-right: 4px;
}

.stats-dot-success {
  background: #14884f;
}

.stats-dot-failed {
  background: #c43838;
}

.notif-distribution {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: #fafbfc;
}

.dist-block {
  flex: 1 1 240px;
  min-width: 220px;
}

.dist-label {
  margin-bottom: 8px;
  color: #606266;
  font-size: 13px;
  font-weight: 600;
}

.dist-bars {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.dist-bar-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dist-bar-track {
  flex: 1;
  height: 14px;
  background: #eef0f4;
  border-radius: 7px;
  overflow: hidden;
}

.dist-bar-fill {
  height: 100%;
  border-radius: 7px;
  transition: width 0.2s ease;
  min-width: 2px;
}

.dist-bar-text {
  flex: 0 0 auto;
  width: 110px;
  color: #606266;
  font-size: 12px;
}

.dist-ratio {
  display: flex;
  height: 22px;
  border-radius: 6px;
  overflow: hidden;
}

.dist-ratio-success {
  display: flex;
  align-items: center;
  justify-content: center;
  background: #14884f;
  color: #fff;
  font-size: 12px;
  min-width: 2px;
}

.dist-ratio-failure {
  display: flex;
  align-items: center;
  justify-content: center;
  background: #c43838;
  color: #fff;
  font-size: 12px;
  min-width: 2px;
}

.dist-ratio-note {
  display: block;
  margin-top: 6px;
  color: #909399;
  font-size: 12px;
}

.notif-empty {
  padding: 40px 0;
}

.notif-day-groups {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.day-group {
  border-left: 3px solid var(--el-color-primary-light-7);
  padding-left: 12px;
}

.day-group-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-regular);
  margin-bottom: 8px;
}

.unread-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-danger);
  margin-right: 6px;
}

.day-item-wrapper {
  cursor: pointer;
}

.day-item {
  width: 100%;
}

.day-item :deep(.el-card__body) {
  padding: 12px;
}

.day-item-main {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.day-item-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.day-item-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 4px;
}

.day-item-content {
  font-size: 13px;
  color: var(--el-text-color-regular);
  white-space: pre-wrap;
  word-break: break-word;
}

.day-item-error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  padding: 6px 8px;
  border-radius: 4px;
}

.notif-timeline {
  padding: 8px 0;
}

.timeline-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 4px;
}

.timeline-content {
  font-size: 13px;
  color: var(--el-text-color-regular);
  margin-bottom: 6px;
  word-break: break-word;
}

.timeline-error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  padding: 6px 8px;
  border-radius: 4px;
}

.detail-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  color: #6b7280;
  font-size: 13px;
}

.detail-title {
  margin: 0 0 8px;
  color: #172033;
  font-size: 16px;
}

.detail-content {
  margin: 0 0 12px;
  color: #4b5563;
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.detail-error {
  border-radius: 6px;
  background: var(--el-color-danger-light-9);
  padding: 10px;
}

.detail-error-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
  color: #6b7280;
  font-size: 12px;
}

.detail-error pre {
  margin: 0;
  color: var(--el-color-danger);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.responsive-table {
  width: 100%;
}

.notif-footer {
  display: flex;
  justify-content: flex-end;
}

@media (max-width: 768px) {
  .notif-actions {
    width: 100%;
  }

  .notif-actions .el-input,
  .notif-actions .el-select,
  .notif-actions .el-date-editor {
    width: 100% !important;
  }
}
</style>
