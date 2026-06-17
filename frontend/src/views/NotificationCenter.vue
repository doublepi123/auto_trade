<template>
  <div class="notif-page">
    <div class="notif-header">
      <h3>通知中心</h3>
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
        />
        <el-button-group>
          <el-button :type="quickFilter === 'all' ? 'primary' : ''" data-testid="notif-filter-all" @click="setQuickFilter('all')">全部</el-button>
          <el-button :type="quickFilter === 'failed' ? 'primary' : ''" data-testid="notif-filter-failed" @click="setQuickFilter('failed')">失败</el-button>
          <el-button :type="quickFilter === 'critical' ? 'primary' : ''" data-testid="notif-filter-critical" @click="setQuickFilter('critical')">CRITICAL</el-button>
          <el-button :type="quickFilter === 'warning' ? 'primary' : ''" data-testid="notif-filter-warning" @click="setQuickFilter('warning')">WARNING</el-button>
          <el-button :type="quickFilter === 'info' ? 'primary' : ''" data-testid="notif-filter-info" @click="setQuickFilter('info')">INFO</el-button>
        </el-button-group>
        <el-button-group>
          <el-button :type="viewMode === 'cards' ? 'primary' : ''" data-testid="notif-view-cards" @click="viewMode = 'cards'">卡片</el-button>
          <el-button :type="viewMode === 'table' ? 'primary' : ''" data-testid="notif-view-table" @click="viewMode = 'table'">表格</el-button>
        </el-button-group>
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <div class="notif-summary" data-testid="notif-summary">
      <el-space wrap :size="8">
        <el-tag type="info">当前页 {{ items.length }}/{{ total }}</el-tag>
        <el-tag type="success">成功 {{ summary.success }}</el-tag>
        <el-tag type="danger">失败 {{ summary.failure }}</el-tag>
        <el-tag type="danger">CRITICAL {{ summary.critical }}</el-tag>
        <el-tag type="warning">WARNING {{ summary.warning }}</el-tag>
        <el-tag type="info">INFO {{ summary.info }}</el-tag>
      </el-space>
    </div>

    <div v-if="items.length === 0" class="notif-empty">
      <el-empty description="没有匹配的通知" />
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
          >
            <el-card shadow="never" class="day-item">
              <div class="day-item-main">
                <span class="day-item-time">{{ formatDateTime(item.created_at) }}</span>
                <el-tag size="small" :type="severityType(item.severity)">{{ item.severity }}</el-tag>
                <el-tag size="small" :type="item.success ? 'success' : 'danger'">{{ item.success ? '成功' : '失败' }}</el-tag>
              </div>
              <div class="day-item-title">{{ item.title }}</div>
              <div class="day-item-content">{{ item.content }}</div>
              <div v-if="item.error" class="day-item-error">{{ item.error }}</div>
            </el-card>
          </div>
        </el-space>
      </div>
    </div>

    <el-table v-else :data="items" size="small" class="responsive-table" v-loading="loading" data-testid="notif-list">
      <el-table-column prop="created_at" label="时间" min-width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="级别" min-width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="severityType(row.severity)">{{ row.severity }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="结果" min-width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.success ? 'success' : 'danger'">{{ row.success ? '成功' : '失败' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="title" label="标题" min-width="140" show-overflow-tooltip />
      <el-table-column prop="content" label="内容" min-width="240" show-overflow-tooltip />
      <el-table-column prop="error" label="错误" min-width="160" show-overflow-tooltip />
    </el-table>

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
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getNotifications } from '../api'
import type { NotificationLogOut } from '../types'
import { resolveErrorMessage } from '../utils/error'

const items = ref<NotificationLogOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const loading = ref(false)
const severityFilter = ref<string>('')
const successFilter = ref<string>('')
const dateRange = ref<string[] | null>(null)
const searchText = ref('')
const quickFilter = ref<'all' | 'failed' | 'critical' | 'warning' | 'info'>('all')
const viewMode = ref<'cards' | 'table'>('cards')
let searchDebounceTimer: number | undefined

const summary = computed(() => {
  let success = 0
  let failure = 0
  let critical = 0
  let warning = 0
  let info = 0
  for (const item of items.value) {
    if (item.success) success += 1
    else failure += 1
    if (item.severity === 'CRITICAL') critical += 1
    else if (item.severity === 'WARNING') warning += 1
    else info += 1
  }
  return { success, failure, critical, warning, info }
})

const dayGroups = computed(() => {
  const groups = new Map<string, NotificationLogOut[]>()
  for (const item of items.value) {
    const day = new Date(item.created_at).toLocaleDateString([], { year: 'numeric', month: '2-digit', day: '2-digit' })
    const list = groups.get(day)
    if (list) list.push(item)
    else groups.set(day, [item])
  }
  return Array.from(groups.entries()).map(([day, groupItems]) => ({ day, items: groupItems }))
})

async function load() {
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
    items.value = data.items
    total.value = data.total
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载通知失败'))
  } finally {
    loading.value = false
  }
}

function handlePage(next: number) {
  page.value = next
  load()
}

function setQuickFilter(value: typeof quickFilter.value) {
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
  page.value = 1
  load()
}

function debouncedLoad() {
  window.clearTimeout(searchDebounceTimer)
  searchDebounceTimer = window.setTimeout(() => {
    page.value = 1
    load()
  }, 300)
}

function severityType(s: string): string {
  if (s === 'CRITICAL') return 'danger'
  if (s === 'WARNING') return 'warning'
  return 'info'
}

function formatDateTime(v: string): string {
  return new Date(v).toLocaleString([], {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

watch(severityFilter, () => { quickFilter.value = 'all'; page.value = 1; load() })
watch(successFilter, () => { quickFilter.value = 'all'; page.value = 1; load() })
watch(dateRange, () => { page.value = 1; load() }, { deep: true })
watch(searchText, debouncedLoad)
onMounted(load)
</script>

<style scoped>
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
