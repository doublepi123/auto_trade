<template>
  <section class="intervention-evidence" data-testid="intervention-evidence-panel">
    <div class="evidence-header">
      <div>
        <h4>干预证据</h4>
        <p class="evidence-caption">仅汇总已持久化的暂停 / 恢复与紧急停止操作证据，不推断空白时段的运行状态</p>
      </div>
      <el-tag v-if="data" size="small" type="info" effect="plain" data-testid="evidence-returned-tag">
        返回 {{ data.returned }} / {{ data.total }} 条
      </el-tag>
    </div>

    <div class="evidence-filters" data-testid="evidence-filters">
      <span data-testid="evidence-from-date">
        <el-date-picker
          v-model="fromDate"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="开始日期"
          size="small"
          class="evidence-date"
          @change="applyFilters"
        />
      </span>
      <span data-testid="evidence-to-date">
        <el-date-picker
          v-model="toDate"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="结束日期"
          size="small"
          class="evidence-date"
          @change="applyFilters"
        />
      </span>
      <el-select
        v-model="limit"
        size="small"
        class="evidence-limit"
        data-testid="evidence-limit"
        @change="applyFilters"
      >
        <el-option :value="100" label="上限 100" />
        <el-option :value="500" label="上限 500" />
        <el-option :value="1000" label="上限 1000" />
      </el-select>
      <el-button size="small" plain :disabled="!fromDate && !toDate" data-testid="evidence-reset" @click="resetFilters">
        重置
      </el-button>
    </div>

    <el-alert
      v-if="error"
      class="evidence-alert"
      type="error"
      :title="error"
      :closable="false"
      show-icon
      data-testid="evidence-error"
    >
      <el-button size="small" type="primary" plain :loading="loading" data-testid="evidence-retry" @click="load">
        重试
      </el-button>
    </el-alert>

    <template v-else>
      <el-alert
        v-if="data && data.scan_truncated"
        class="evidence-alert"
        type="warning"
        :closable="false"
        show-icon
        data-testid="evidence-incomplete-alert"
        :title="`配对上下文超出扫描上限：本次配对不完整，所有时长已抑制，依赖上下文的状态显示为「未知」。总数 ${data.total} 条仍为精确值。`"
      />
      <el-alert
        v-if="data && responseLimitTruncated"
        class="evidence-alert"
        type="info"
        :closable="false"
        show-icon
        data-testid="evidence-truncated-alert"
        :title="`结果超出返回上限：仅显示前 ${data.returned} 条（符合筛选共 ${data.total} 条）。`"
      />

      <div v-if="loading && !data" class="evidence-state" data-testid="evidence-loading">加载中…</div>

      <template v-else-if="data">
        <div class="evidence-meta" data-testid="evidence-summary">
          <span>证据总数 <strong>{{ data.total }}</strong></span>
          <span>筛选后已扫描 <strong>{{ data.filtered_scanned }}</strong></span>
          <span>配对上下文扫描 <strong>{{ data.pairing_context_scanned }}</strong></span>
          <span v-if="filtersEchoLabel" data-testid="evidence-filters-echo">{{ filtersEchoLabel }}</span>
        </div>

        <div class="evidence-counts" data-testid="evidence-counts">
          <el-tag size="small" type="success" effect="plain">已配对证据 {{ data.summary.paired_count }}</el-tag>
          <el-tag size="small" type="warning" effect="plain">未关闭 {{ data.summary.open_count }}</el-tag>
          <el-tag size="small" type="warning" effect="plain">无配对关闭 {{ data.summary.unmatched_close_count }}</el-tag>
          <el-tag size="small" type="danger" effect="plain">歧义 {{ data.summary.ambiguous_count }}</el-tag>
          <el-tag size="small" type="info" effect="plain">未知 {{ data.summary.unknown_count }}</el-tag>
          <span class="evidence-duration-total" data-testid="evidence-paired-duration">
            已配对时长合计
            <strong v-if="data.classification_complete">{{ formatDurationSeconds(data.summary.paired_duration_seconds) }}</strong>
            <strong v-else>—（已抑制）</strong>
          </span>
        </div>
        <p class="evidence-counts-note" data-testid="evidence-counts-note">
          状态计数基于已扫描且符合筛选的 {{ data.summary.scanned_evidence }} 条证据<template v-if="!data.classification_complete">；配对不完整时仅描述该部分</template>
        </p>

        <el-table
          v-if="data.items.length > 0"
          :data="tableRows"
          size="small"
          class="responsive-table evidence-table"
          data-testid="evidence-table"
          v-loading="loading"
          row-key="row_key"
        >
          <el-table-column label="时间" min-width="170">
            <template #default="{ row }">{{ formatDateTime(row.timestamp) }}</template>
          </el-table-column>
          <el-table-column label="类别" min-width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.family === 'kill_switch' ? 'danger' : 'primary'" effect="plain">
                {{ familyLabel(row.family) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="动作" min-width="110">
            <template #default="{ row }">{{ kindLabel(row) }}</template>
          </el-table-column>
          <el-table-column label="方向" width="64">
            <template #default="{ row }">{{ row.direction === 'open' ? '开始' : '结束' }}</template>
          </el-table-column>
          <el-table-column label="来源" width="64">
            <template #default="{ row }">{{ row.source === 'audit' ? '手动' : '自动' }}</template>
          </el-table-column>
          <el-table-column label="配对状态" min-width="104">
            <template #default="{ row }">
              <el-tag size="small" :type="pairingTagType(row.pairing_status)" data-testid="evidence-status-tag">
                {{ pairingLabel(row.pairing_status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="时长" min-width="100">
            <template #default="{ row }">
              <span data-testid="evidence-duration">{{ formatDurationSeconds(row.duration_seconds) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="原因代码" min-width="190">
            <template #default="{ row }">
              <code class="evidence-reason" data-testid="evidence-reason">{{ row.reason || '—' }}</code>
            </template>
          </el-table-column>
          <el-table-column label="操作者" min-width="90">
            <template #default="{ row }">{{ row.actor_hash ? row.actor_hash.slice(0, 8) : '—' }}</template>
          </el-table-column>
        </el-table>
        <p v-else class="evidence-state" data-testid="evidence-empty">当前条件下暂无干预证据</p>

        <details v-if="data.pairing_rule" class="evidence-rule" data-testid="evidence-rule">
          <summary>配对规则说明</summary>
          <p>{{ data.pairing_rule }}</p>
        </details>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getInterventionEvidence } from '../api'
import type {
  InterventionEvidenceResponse,
  InterventionEvidenceRow,
  InterventionPairingStatus,
} from '../types'
import { auditActionLabel, tradeEventTypeLabel } from '../utils/labels'
import { formatDurationSeconds } from '../utils/format'
import { resolveErrorMessage } from '../utils/error'

/**
 * Curated intervention evidence surface for the Decision Timeline. Complements
 * the raw event table below it: the raw table stays the unfiltered record,
 * this panel answers "what pause/resume and kill-switch interventions are on
 * record, how do they pair up, and how complete is that evidence". Every
 * control issues GET requests only — filters change query parameters, never
 * server state. No pause/resume/kill-switch actions exist here by design.
 */
type EvidenceRow = InterventionEvidenceRow & { row_key: string }

const data = ref<InterventionEvidenceResponse | null>(null)
const loading = ref(false)
const error = ref('')
const fromDate = ref('')
const toDate = ref('')
const limit = ref(500)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await getInterventionEvidence({
      from_date: fromDate.value || undefined,
      to_date: toDate.value || undefined,
      limit: limit.value,
    })
  } catch (err) {
    error.value = resolveErrorMessage(err, '加载干预证据失败')
    data.value = null
  } finally {
    loading.value = false
  }
}

function applyFilters() {
  void load()
}

function resetFilters() {
  fromDate.value = ''
  toDate.value = ''
  void load()
}

const filtersEchoLabel = computed(() => {
  // Render only what the backend echoes. A missing key means "not applied" —
  // never substitute the local filter inputs, which could diverge from what
  // the server actually used.
  const filters = data.value?.filters
  if (!filters) return ''
  const parts: string[] = []
  const from = filters.from_date ?? ''
  const to = filters.to_date ?? ''
  parts.push(from || to ? `筛选 ${from || '…'} 至 ${to || '…'}` : '未设置日期筛选')
  if (typeof filters.limit === 'number') parts.push(`上限 ${filters.limit}`)
  return parts.join(' · ')
})

/** Response-limit truncation is its own fact: the bounded response cut rows
 * that matched the filters. Independent of scan-cap truncation (`scan_truncated`),
 * and both can be true at once — the two alerts render independently. */
const responseLimitTruncated = computed(
  () => (data.value ? data.value.returned < data.value.filtered_scanned : false),
)

const tableRows = computed<EvidenceRow[]>(() =>
  (data.value?.items ?? []).map((item) => ({
    ...item,
    row_key: `${item.source}-${item.source_id}`,
  })),
)

const PAIRING_LABELS: Record<InterventionPairingStatus, string> = {
  PAIRED: '已配对',
  OPEN: '未关闭',
  UNMATCHED_CLOSE: '无配对关闭',
  AMBIGUOUS: '歧义',
  UNKNOWN: '未知',
}

const PAIRING_TAG_TYPES: Record<InterventionPairingStatus, string> = {
  PAIRED: 'success',
  OPEN: 'warning',
  UNMATCHED_CLOSE: 'warning',
  AMBIGUOUS: 'danger',
  UNKNOWN: 'info',
}

function pairingLabel(status: InterventionPairingStatus): string {
  return PAIRING_LABELS[status] ?? status
}

function pairingTagType(status: InterventionPairingStatus): string {
  return PAIRING_TAG_TYPES[status] ?? 'info'
}

function familyLabel(family: string): string {
  if (family === 'kill_switch') return '紧急停止'
  if (family === 'pause') return '暂停/恢复'
  return family
}

function kindLabel(row: EvidenceRow): string {
  return row.source === 'audit' ? auditActionLabel(row.kind) : tradeEventTypeLabel(row.kind)
}

function formatDateTime(value: string): string {
  const time = new Date(value)
  if (Number.isNaN(time.getTime())) return value
  return time.toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

onMounted(() => {
  void load()
})

// The Decision Timeline's existing 刷新 button reloads this panel through
// this exposed method — no separate control is added.
defineExpose({ reload: load })
</script>

<style scoped>
.intervention-evidence {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 12px;
  background: #fafbfc;
}

.evidence-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.evidence-header h4 {
  margin: 0;
  color: #4b5563;
  font-size: 13px;
  font-weight: 700;
}

.evidence-caption {
  margin: 4px 0 0;
  color: #909399;
  font-size: 12px;
}

.evidence-filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.evidence-date {
  width: 150px;
}

.evidence-limit {
  width: 110px;
}

.evidence-alert {
  margin-bottom: 0;
}

.evidence-state {
  padding: 8px 0;
  color: #909399;
  font-size: 12px;
}

.evidence-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  color: #6b7280;
  font-size: 12px;
}

.evidence-meta strong {
  color: #172033;
}

.evidence-counts {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.evidence-duration-total {
  color: #6b7280;
  font-size: 12px;
}

.evidence-duration-total strong {
  color: #172033;
}

.evidence-counts-note {
  margin: 0;
  color: #909399;
  font-size: 11px;
}

.evidence-reason {
  font-size: 11px;
  color: #4b5563;
  word-break: break-all;
}

.evidence-rule {
  color: #6b7280;
  font-size: 12px;
}

.evidence-rule summary {
  cursor: pointer;
  color: #909399;
}

.evidence-rule p {
  margin: 6px 0 0;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

@media (max-width: 520px) {
  .evidence-date,
  .evidence-limit {
    width: 100%;
  }
}
</style>
