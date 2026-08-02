<template>
  <div class="cron-health" data-testid="dashboard-cron-health">
    <div class="cron-health-header">
      <h5>定时任务健康</h5>
      <span v-if="snapshot" class="cron-health-checked" data-testid="cron-health-checked-at">
        快照 {{ snapshotAsOfLabel }}
      </span>
    </div>

    <div v-if="loading && !snapshot" class="cron-health-state" data-testid="cron-health-loading">加载中…</div>
    <div v-else-if="error" class="cron-health-state cron-health-error" data-testid="cron-health-error">{{ error }}</div>
    <div v-else-if="snapshot && snapshot.jobs.length === 0" class="cron-health-state" data-testid="cron-health-empty">
      暂无已注册的定时任务
    </div>

    <template v-else-if="snapshot">
      <div class="cron-health-summary" data-testid="cron-health-summary">
        <el-tag size="small" type="info" effect="plain">共 {{ snapshot.jobs.length }} 个任务</el-tag>
        <el-tag v-if="statusCounts.healthy" size="small" type="success" effect="plain">健康 {{ statusCounts.healthy }}</el-tag>
        <el-tag v-if="statusCounts.failing" size="small" type="danger" effect="plain">失败 {{ statusCounts.failing }}</el-tag>
        <el-tag v-if="statusCounts.stale" size="small" type="warning" effect="plain">过期 {{ statusCounts.stale }}</el-tag>
        <el-tag v-if="statusCounts.pending" size="small" type="info" effect="plain">等待中 {{ statusCounts.pending }}</el-tag>
        <el-tag v-if="statusCounts.disabled" size="small" type="info" effect="plain">已禁用 {{ statusCounts.disabled }}</el-tag>
        <el-tag v-if="statusCounts.unknown" size="small" type="info" effect="plain">未知 {{ statusCounts.unknown }}</el-tag>
        <el-tag
          v-if="staleCount"
          size="small"
          type="warning"
          effect="plain"
          data-testid="cron-health-stale-count"
        >心跳过期 {{ staleCount }}</el-tag>
      </div>

      <ul class="cron-health-jobs">
        <li
          v-for="job in snapshot.jobs"
          :key="job.name"
          class="cron-job"
          :data-testid="`cron-health-job-${job.name}`"
        >
          <div class="cron-job-top">
            <strong class="cron-job-name" data-testid="cron-job-name">{{ job.name }}</strong>
            <span class="cron-job-tags">
              <el-tag size="small" :type="statusTagType(job.status)" data-testid="cron-job-status">
                {{ statusLabel(job.status) }}
              </el-tag>
              <el-tag
                v-if="job.stale && job.status !== 'stale'"
                size="small"
                type="warning"
                effect="plain"
                data-testid="cron-job-stale"
              >心跳过期</el-tag>
            </span>
          </div>
          <div class="cron-job-grid">
            <div class="cron-job-item">
              <span>启用状态</span>
              <strong data-testid="cron-job-enabled">{{ enabledLabel(job.enabled) }}</strong>
            </div>
            <div class="cron-job-item">
              <span>预期间隔</span>
              <strong data-testid="cron-job-interval">{{ intervalLabel(job.expected_interval_seconds) }}</strong>
            </div>
            <div class="cron-job-item">
              <span>最近结果</span>
              <strong data-testid="cron-job-outcome">{{ outcomeLabel(job.last_outcome) }}</strong>
            </div>
            <div class="cron-job-item">
              <span>Tick / 失败</span>
              <strong data-testid="cron-job-ticks">{{ job.tick_count }} / {{ job.failure_count }}</strong>
            </div>
            <div class="cron-job-item">
              <span>最近成功</span>
              <strong data-testid="cron-job-last-success">{{ timestampLabel(job.last_success_at) }}</strong>
            </div>
            <div class="cron-job-item">
              <span>最近失败</span>
              <strong data-testid="cron-job-last-failure">{{ timestampLabel(job.last_failure_at) }}</strong>
            </div>
          </div>
          <p v-if="job.last_failure_code" class="cron-job-failure" data-testid="cron-job-failure-code">
            最近失败代码 {{ job.last_failure_code }}
          </p>
        </li>
      </ul>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { CronHealthSnapshot, CronJobOutcome, CronJobStatus } from '../types'
import { relativeAgeLabel } from '../utils/time'

/**
 * Read-only cron job health surface for the Dashboard diagnostics panel.
 * Mirrors the backend verdicts exactly (disabled/healthy/failing/stale/
 * pending/unknown) and keeps staleness as its own dimension — a failing job
 * that has also missed heartbeats shows both. Offers no run/restart/register
 * controls: the endpoint is pure observation and the UI stays that way.
 */
const props = withDefaults(
  defineProps<{
    snapshot: CronHealthSnapshot | null
    loading?: boolean
    error?: string
    /** 1s ticker from the parent so freshness labels age without a refetch. */
    nowTick?: number
  }>(),
  {
    loading: false,
    error: '',
    nowTick: 0,
  },
)

const STATUS_LABELS: Record<CronJobStatus, string> = {
  healthy: '健康',
  failing: '失败',
  stale: '过期',
  pending: '等待中',
  disabled: '已禁用',
  unknown: '未知',
}

const STATUS_TAG_TYPES: Record<CronJobStatus, string> = {
  healthy: 'success',
  failing: 'danger',
  stale: 'warning',
  pending: 'info',
  disabled: 'info',
  unknown: 'info',
}

function statusLabel(status: CronJobStatus): string {
  return STATUS_LABELS[status] ?? status
}

function statusTagType(status: CronJobStatus): string {
  return STATUS_TAG_TYPES[status] ?? 'info'
}

function enabledLabel(enabled: boolean | null): string {
  if (enabled === null || enabled === undefined) return '未知'
  return enabled ? '已启用' : '已禁用'
}

function outcomeLabel(outcome: CronJobOutcome): string {
  if (outcome === 'success') return '成功'
  if (outcome === 'failure') return '失败'
  return '暂无'
}

function intervalLabel(seconds: number | null): string {
  if (seconds === null || seconds === undefined || seconds <= 0) return '未知'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  const minutes = seconds / 60
  if (minutes < 60) return `${Number.isInteger(minutes) ? minutes : minutes.toFixed(1)} 分钟`
  const hours = minutes / 60
  return `${Number.isInteger(hours) ? hours : hours.toFixed(1)} 小时`
}

function ageSecondsOf(iso: string): number | null {
  const time = new Date(iso).getTime()
  if (Number.isNaN(time) || !props.nowTick) return null
  return Math.max(0, Math.floor((props.nowTick - time) / 1000))
}

function timestampLabel(iso: string | null): string {
  if (!iso) return '—'
  const time = new Date(iso)
  if (Number.isNaN(time.getTime())) return iso
  const clock = time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  const age = ageSecondsOf(iso)
  return age === null ? clock : `${clock} · ${relativeAgeLabel(age)}`
}

const snapshotAsOfLabel = computed(() => {
  const at = props.snapshot?.as_of
  if (!at) return ''
  return timestampLabel(at)
})

const statusCounts = computed(() => {
  const counts: Record<CronJobStatus, number> = {
    healthy: 0,
    failing: 0,
    stale: 0,
    pending: 0,
    disabled: 0,
    unknown: 0,
  }
  for (const job of props.snapshot?.jobs ?? []) {
    if (job.status in counts) {
      counts[job.status] += 1
    } else {
      counts.unknown += 1
    }
  }
  return counts
})

/**
 * Staleness is a heartbeat dimension independent of the latest-outcome
 * verdict: a failing job whose ticks have also stopped arriving counts here
 * too. Counted from `job.stale`, never inferred from `status`.
 */
const staleCount = computed(
  () => (props.snapshot?.jobs ?? []).filter((job) => job.stale).length,
)
</script>

<style scoped>
.cron-health {
  margin-bottom: 14px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
  background: #fbfcfe;
}

.cron-health-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.cron-health-header h5 {
  margin: 0;
  color: #334155;
  font-size: 13px;
  font-weight: 600;
}

.cron-health-checked {
  color: #909399;
  font-size: 12px;
}

.cron-health-state {
  padding: 8px 0;
  color: #909399;
  font-size: 12px;
}

.cron-health-error {
  color: var(--el-color-danger);
}

.cron-health-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}

.cron-health-jobs {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.cron-job {
  border: 1px solid #e8edf4;
  border-radius: 6px;
  padding: 10px;
  background: #fff;
}

.cron-job-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.cron-job-name {
  color: #172033;
  font-size: 13px;
  word-break: break-all;
}

.cron-job-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.cron-job-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
  margin-top: 8px;
}

.cron-job-item {
  border-radius: 6px;
  padding: 6px 8px;
  background: #f4f6fa;
  min-width: 0;
}

.cron-job-item span {
  display: block;
  color: #6b7280;
  font-size: 11px;
}

.cron-job-item strong {
  display: block;
  margin-top: 2px;
  color: #172033;
  font-size: 12px;
  font-weight: 600;
  word-break: break-all;
}

.cron-job-failure {
  margin: 8px 0 0;
  color: var(--el-color-danger);
  font-size: 12px;
  word-break: break-all;
}

@media (max-width: 768px) {
  .cron-health-jobs {
    grid-template-columns: 1fr;
  }

  .cron-job-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
