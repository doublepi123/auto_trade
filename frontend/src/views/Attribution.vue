<template>
  <div class="attribution-page" data-testid="attribution-page">
    <div class="page-header">
      <div>
        <h3>绩效归因分析</h3>
        <p>按标的 / 方向 / 退出原因 / 市场拆解 PnL 来源</p>
      </div>
      <div class="page-actions">
        <el-select v-model="days" style="width: 130px" data-testid="attribution-days" @change="reload">
          <el-option label="近 7 天" :value="7" />
          <el-option label="近 14 天" :value="14" />
          <el-option label="近 30 天" :value="30" />
          <el-option label="近 60 天" :value="60" />
          <el-option label="近 90 天" :value="90" />
        </el-select>
        <el-button type="primary" :loading="loading" data-testid="attribution-refresh" @click="reload">刷新</el-button>
      </div>
    </div>

    <el-row :gutter="12" v-loading="loading">
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="总 PnL" :value="result?.total_pnl ?? 0" :precision="2" />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="总交易" :value="result?.total_trades ?? 0" />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="胜率" :value="winRatePct" suffix="%" />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="期间天数" :value="result?.period_days ?? 0" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="12" v-loading="loading">
      <el-col :xs="24" :sm="12">
        <el-card shadow="never" class="section-card">
          <template #header>按标的</template>
          <el-table :data="symbolRows" size="small" max-height="280">
            <el-table-column prop="key" label="标的" min-width="110" />
            <el-table-column label="PnL" min-width="100">
              <template #default="{ row }">
                <span :class="row.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'">{{ row.total_pnl.toFixed(2) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易" width="70" />
            <el-table-column label="胜率" width="80">
              <template #default="{ row }">{{ formatWinRate(row) }}</template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!symbolRows.length" description="无数据" :image-size="40" />
        </el-card>
      </el-col>

      <el-col :xs="24" :sm="12">
        <el-card shadow="never" class="section-card">
          <template #header>按方向</template>
          <el-table :data="directionRows" size="small" max-height="280">
            <el-table-column prop="key" label="方向" min-width="110" />
            <el-table-column label="PnL" min-width="100">
              <template #default="{ row }">
                <span :class="row.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'">{{ row.total_pnl.toFixed(2) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易" width="80" />
          </el-table>
          <el-empty v-if="!directionRows.length" description="无数据" :image-size="40" />
        </el-card>
      </el-col>

      <el-col :xs="24" :sm="12">
        <el-card shadow="never" class="section-card">
          <template #header>按退出原因</template>
          <el-table :data="exitReasonRows" size="small" max-height="280">
            <el-table-column prop="key" label="退出原因" min-width="150" show-overflow-tooltip />
            <el-table-column label="PnL" min-width="100">
              <template #default="{ row }">
                <span :class="row.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'">{{ row.total_pnl.toFixed(2) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易" width="80" />
          </el-table>
          <el-empty v-if="!exitReasonRows.length" description="无数据" :image-size="40" />
        </el-card>
      </el-col>

      <el-col :xs="24" :sm="12">
        <el-card shadow="never" class="section-card">
          <template #header>按市场</template>
          <el-table :data="sessionRows" size="small" max-height="280">
            <el-table-column prop="key" label="市场" min-width="110" />
            <el-table-column label="PnL" min-width="100">
              <template #default="{ row }">
                <span :class="row.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'">{{ row.total_pnl.toFixed(2) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易" width="80" />
          </el-table>
          <el-empty v-if="!sessionRows.length" description="无数据" :image-size="40" />
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="section-card" v-loading="loading">
      <template #header>Top 贡献者</template>
      <el-table :data="contributors" size="small" data-testid="attribution-top-contributors">
        <el-table-column prop="symbol" label="标的" min-width="110" />
        <el-table-column label="PnL" min-width="110">
          <template #default="{ row }">
            <span :class="row.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'">{{ row.total_pnl.toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="trade_count" label="交易" width="80" />
        <el-table-column label="胜率" width="80">
          <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
        </el-table-column>
        <el-table-column label="平均持仓(分)" min-width="110">
          <template #default="{ row }">{{ row.avg_holding_minutes.toFixed(0) }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && contributors.length === 0" description="无贡献者数据" />
    </el-card>

    <el-card shadow="never" class="section-card" v-loading="loading">
      <template #header>每日 PnL</template>
      <div v-if="dayBars.length" class="day-bar-list" data-testid="attribution-daily-pnl">
        <div v-for="d in dayBars" :key="d.date" class="day-bar-row">
          <span class="day-bar-date">{{ d.date }}</span>
          <div class="day-bar-track">
            <div
              class="day-bar-zero"
              :style="{ left: `${d.zeroPct}%` }"
            />
            <div
              class="day-bar-fill"
              :class="d.pnl >= 0 ? 'day-bar-pos' : 'day-bar-neg'"
              :style="d.barStyle"
            />
          </div>
          <span class="day-bar-value" :class="d.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'">{{ d.pnl.toFixed(2) }}</span>
        </div>
      </div>
      <el-empty v-else description="无每日 PnL 数据" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getPnlAttribution, getTopContributors } from '../api/attribution'
import type { AttributionResult, PnlBucket, TopContributor } from '../api/attribution'
import { resolveErrorMessage } from '../utils/error'

interface BucketRow extends PnlBucket {
  key: string
}

interface DayBar {
  date: string
  pnl: number
  zeroPct: number
  barStyle: { left: string; width: string; right: string }
}

const result = ref<AttributionResult | null>(null)
const contributors = ref<TopContributor[]>([])
const loading = ref(false)
const days = ref(30)

const winRatePct = computed(() => ((result.value?.win_rate ?? 0) * 100).toFixed(1))

function toRows(map: Record<string, PnlBucket> | undefined): BucketRow[] {
  if (!map) return []
  return Object.entries(map)
    .map(([key, v]) => ({ key, ...v }))
    .sort((a, b) => b.total_pnl - a.total_pnl)
}

const symbolRows = computed(() => toRows(result.value?.by_symbol))
const directionRows = computed(() => toRows(result.value?.by_direction))
const exitReasonRows = computed(() => toRows(result.value?.by_exit_reason))
const sessionRows = computed(() => toRows(result.value?.by_session))

function formatWinRate(row: BucketRow): string {
  if (row.trade_count === 0 || row.win_count == null) return '—'
  return `${((row.win_count / row.trade_count) * 100).toFixed(1)}%`
}

const dayBars = computed<DayBar[]>(() => {
  const daysData = result.value?.by_day ?? []
  if (daysData.length === 0) return []
  const maxAbs = daysData.reduce((m, d) => Math.max(m, Math.abs(d.pnl)), 0) || 1
  return daysData.map((d) => {
    const halfPct = (Math.abs(d.pnl) / maxAbs) * 50
    const pnlPositive = d.pnl >= 0
    return {
      date: d.date,
      pnl: d.pnl,
      zeroPct: 50,
      barStyle: pnlPositive
        ? { left: '50%', width: `${halfPct}%`, right: 'auto' }
        : { left: 'auto', width: `${halfPct}%`, right: '50%' },
    }
  })
})

async function reload() {
  loading.value = true
  try {
    const [attr, top] = await Promise.all([
      getPnlAttribution({ days: days.value }),
      getTopContributors({ days: days.value }),
    ])
    result.value = attr
    contributors.value = top
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载归因数据失败'))
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.attribution-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
  background: #fff;
  min-height: calc(100vh - 120px);
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.page-header h3 {
  margin: 0;
}

.page-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.page-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.section-card {
  margin-top: 0;
}

.pnl-pos {
  color: #67c23a;
  font-weight: 600;
}

.pnl-neg {
  color: #f56c6c;
  font-weight: 600;
}

.day-bar-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.day-bar-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.day-bar-date {
  width: 90px;
  color: #606266;
  flex-shrink: 0;
}

.day-bar-track {
  position: relative;
  flex: 1;
  height: 14px;
  background: #f0f2f5;
  border-radius: 3px;
  overflow: hidden;
}

.day-bar-zero {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1px;
  background: #c0c4cc;
}

.day-bar-fill {
  position: absolute;
  top: 0;
  bottom: 0;
  border-radius: 2px;
}

.day-bar-pos {
  background: #67c23a;
}

.day-bar-neg {
  background: #f56c6c;
}

.day-bar-value {
  width: 80px;
  text-align: right;
  flex-shrink: 0;
}

@media (max-width: 720px) {
  .page-header {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
