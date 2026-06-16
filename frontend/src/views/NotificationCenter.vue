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
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-table :data="items" size="small" class="responsive-table" v-loading="loading">
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
import { ref, watch, onMounted } from 'vue'
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

watch(severityFilter, () => { page.value = 1; load() })
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

.responsive-table {
  width: 100%;
}

.notif-footer {
  display: flex;
  justify-content: flex-end;
}
</style>
