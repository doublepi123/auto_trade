<template>
  <div class="reconciliation-status" data-testid="reconciliation-status">
    <div class="recon-header">
      <span class="recon-label">对账状态</span>
      <el-tag :type="gateTagType" size="small" data-testid="recon-gate-tag">
        {{ gateLabel }}
      </el-tag>
    </div>

    <div v-if="reconciliationStatus.last_evidence" class="recon-last-evidence">
      <div class="recon-evidence-row">
        <span>上次对账</span>
        <strong>{{ formatTime(reconciliationStatus.last_evidence.timestamp) }}</strong>
      </div>
      <div class="recon-evidence-row">
        <span>事件类型</span>
        <el-tag :type="reconciliationStatus.last_evidence.passed ? 'success' : 'danger'" size="small">
          {{ reconciliationStatus.last_evidence.event_type }}
        </el-tag>
      </div>
      <div v-if="reconciliationStatus.last_evidence.position_count !== null" class="recon-evidence-row">
        <span>持仓数</span>
        <strong>{{ reconciliationStatus.last_evidence.position_count }}</strong>
      </div>
      <div v-if="reconciliationStatus.last_evidence.order_count !== null" class="recon-evidence-row">
        <span>订单数</span>
        <strong>{{ reconciliationStatus.last_evidence.order_count }}</strong>
      </div>
      <div v-if="reconciliationStatus.last_evidence.drift_summary" class="recon-evidence-row drift-summary">
        <span>偏差</span>
        <strong class="drift-text">{{ reconciliationStatus.last_evidence.drift_summary }}</strong>
      </div>
    </div>

    <div v-if="reconciliationStatus.reconciliation_gate !== 'passed'" class="recon-warning">
      <el-alert
        :type="reconciliationStatus.reconciliation_gate === 'failed' ? 'error' : 'warning'"
        :title="reconciliationStatus.reconciliation_gate === 'failed' ? '对账未通过，订单已阻止' : '对账等待中，订单已阻止'"
        :closable="false"
        show-icon
      />
      <div v-if="reconciliationStatus.force_resume_available" class="recon-force-resume">
        <el-button
          size="small"
          type="warning"
          plain
          @click="$emit('forceResume', 'operator-forced-resume')"
          data-testid="recon-force-resume-btn"
        >
          强制恢复
        </el-button>
      </div>
    </div>

    <div v-if="reconciliationStatus.recent_evidence.length > 1" class="recon-recent">
      <el-button link size="small" @click="showRecent = !showRecent" data-testid="recon-toggle-recent">
        {{ showRecent ? '收起' : '展开' }}最近对账记录 ({{ reconciliationStatus.recent_evidence.length }})
      </el-button>
      <el-table
        v-if="showRecent"
        :data="reconciliationStatus.recent_evidence"
        size="small"
        class="responsive-table"
        data-testid="recon-evidence-table"
      >
        <el-table-column prop="timestamp" label="时间" min-width="140">
          <template #default="{ row }">{{ formatTime(row.timestamp) }}</template>
        </el-table-column>
        <el-table-column prop="event_type" label="事件" min-width="100" />
        <el-table-column label="通过" min-width="70">
          <template #default="{ row }">
            <el-tag :type="row.passed ? 'success' : 'danger'" size="small">
              {{ row.passed ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="position_count" label="持仓" min-width="60" />
        <el-table-column prop="order_count" label="订单" min-width="60" />
        <el-table-column prop="drift_summary" label="偏差" min-width="120" show-overflow-tooltip />
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useReconciliationStatus } from '../composables/useReconciliationStatus'

defineEmits<{
  forceResume: [reason: string]
}>()

const {
  reconciliationStatus,
  reconciliationLoading,
  fetchStatus,
  gateLabel,
  gateTagType,
} = useReconciliationStatus()

const showRecent = ref(false)

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// Fetch on mount
fetchStatus()
</script>

<style scoped>
.reconciliation-status {
  padding: 12px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.recon-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.recon-label {
  font-weight: 600;
  font-size: 14px;
}

.recon-last-evidence {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-bottom: 8px;
}

.recon-evidence-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.recon-evidence-row span {
  color: var(--el-text-color-secondary);
}

.drift-summary {
  width: 100%;
}

.drift-text {
  color: var(--el-color-danger);
  font-size: 12px;
  word-break: break-all;
}

.recon-warning {
  margin: 8px 0;
}

.recon-force-resume {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
}

.recon-recent {
  margin-top: 8px;
}
</style>