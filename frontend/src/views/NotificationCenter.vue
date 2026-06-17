<template>
  <div class="notif-page">
    <div class="notif-header">
      <h3>通知中心</h3>
      <div class="notif-actions">
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索标题/内容/错误"
          style="width: 220px"
          data-testid="notif-search"
        />
        <el-select v-model="severityFilter" placeholder="全部级别" clearable style="width: 140px" data-testid="notif-severity">
          <el-option label="INFO" value="INFO" />
          <el-option label="WARNING" value="WARNING" />
          <el-option label="CRITICAL" value="CRITICAL" />
        </el-select>
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <div class="notif-summary" data-testid="notif-summary">
      <el-card shadow="never"><el-statistic title="当前页结果" :value="filteredItems.length" /></el-card>
      <el-card shadow="never"><el-statistic title="当前页成功" :value="notificationStats.success" /></el-card>
      <el-card shadow="never"><el-statistic title="当前页失败" :value="notificationStats.failed" /></el-card>
      <el-card shadow="never"><el-statistic title="当前页 CRITICAL" :value="notificationStats.critical" /></el-card>
      <el-card shadow="never"><el-statistic title="当前页 WARNING" :value="notificationStats.warning" /></el-card>
      <el-card shadow="never"><el-statistic title="当前页 INFO" :value="notificationStats.info" /></el-card>
    </div>

    <div class="notif-filter-row">
      <el-button size="small" :type="quickFilter === 'all' ? 'primary' : 'default'" data-testid="notif-filter-all" @click="setQuickFilter('all')">全部</el-button>
      <el-button size="small" :type="quickFilter === 'failed' ? 'primary' : 'default'" data-testid="notif-filter-failed" @click="setQuickFilter('failed')">失败</el-button>
      <el-button size="small" :type="quickFilter === 'CRITICAL' ? 'primary' : 'default'" @click="setQuickFilter('CRITICAL')">CRITICAL</el-button>
      <el-button size="small" :type="quickFilter === 'WARNING' ? 'primary' : 'default'" @click="setQuickFilter('WARNING')">WARNING</el-button>
      <el-button size="small" :type="quickFilter === 'INFO' ? 'primary' : 'default'" @click="setQuickFilter('INFO')">INFO</el-button>
      <span class="result-note">{{ resultNote }}</span>
    </div>

    <div class="notif-day-groups" data-testid="notif-day-groups">
      <el-card v-for="group in groupedNotifications" :key="group.day" shadow="never" class="day-card">
        <template #header>{{ group.day }} · {{ group.items.length }} 条</template>
        <div class="day-items">
          <span v-for="item in group.items" :key="item.id">{{ item.title }}</span>
        </div>
      </el-card>
      <el-empty v-if="filteredItems.length === 0" :description="emptyDescription" />
    </div>

    <el-table :data="filteredItems" size="small" class="responsive-table" v-loading="loading" data-testid="notif-list">
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
const keyword = ref('')
type QuickFilter = 'all' | 'failed' | 'CRITICAL' | 'WARNING' | 'INFO'
const quickFilter = ref<QuickFilter>('all')

const filteredItems = computed(() => {
  const term = keyword.value.trim().toLowerCase()
  return items.value.filter((item) => {
    const matchesQuickFilter = quickFilter.value === 'all'
      || (quickFilter.value === 'failed' && !item.success)
      || item.severity === quickFilter.value
    if (!matchesQuickFilter) return false
    if (!term) return true
    return [item.title, item.content, item.error, item.severity]
      .some((value) => value.toLowerCase().includes(term))
  })
})

const notificationStats = computed(() => ({
  success: filteredItems.value.filter((item) => item.success).length,
  failed: filteredItems.value.filter((item) => !item.success).length,
  critical: filteredItems.value.filter((item) => item.severity === 'CRITICAL').length,
  warning: filteredItems.value.filter((item) => item.severity === 'WARNING').length,
  info: filteredItems.value.filter((item) => item.severity === 'INFO').length,
}))

const groupedNotifications = computed(() => {
  const groups = new Map<string, NotificationLogOut[]>()
  for (const item of filteredItems.value) {
    const day = dayLabel(item.created_at)
    groups.set(day, [...(groups.get(day) ?? []), item])
  }
  return [...groups.entries()].map(([day, groupItems]) => ({ day, items: groupItems }))
})

const resultNote = computed(() => `当前页 ${items.value.length} 条，筛选后 ${filteredItems.value.length} 条`)
const emptyDescription = computed(() => (keyword.value.trim() ? '当前页搜索/筛选无通知' : '当前页筛选无通知'))

async function load() {
  loading.value = true
  try {
    const data = await getNotifications({
      page: page.value,
      page_size: pageSize.value,
      ...(severityFilter.value ? { severity: severityFilter.value } : {}),
    })
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

function setQuickFilter(next: QuickFilter) {
  quickFilter.value = next
  if (next === 'all' || next === 'CRITICAL' || next === 'WARNING' || next === 'INFO') {
    severityFilter.value = ''
  }
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

function dayLabel(v: string): string {
  const [year, month, day] = v.slice(0, 10).split('-')
  return `${year}-${month}-${day} (${month}/${day})`
}

watch(severityFilter, () => {
  page.value = 1
  if (severityFilter.value) quickFilter.value = 'all'
  load()
})
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
}

.notif-header h3 {
  margin: 0;
}

.notif-actions {
  display: flex;
  gap: 8px;
}

.notif-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 8px;
}

.notif-filter-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.result-note {
  color: #6b7280;
  font-size: 13px;
}

.notif-day-groups {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px;
}

.day-card :deep(.el-card__header) {
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
}

.day-items {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: #606266;
  font-size: 13px;
}

.responsive-table {
  width: 100%;
}

.notif-footer {
  display: flex;
  justify-content: flex-end;
}
</style>
