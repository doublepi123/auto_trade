<script setup lang="ts">
import { ref } from 'vue'
import { getReentryAnalysisSummary, type ReentryAnalysisResult, type ReentryBucket } from '../api/reentryAnalysis'

const loading = ref(false)
const result = ref<ReentryAnalysisResult | null>(null)
const days = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getReentryAnalysisSummary({ days: days.value })
  } finally {
    loading.value = false
  }
}

function wr(b: ReentryBucket): string {
  return b.win_rate != null ? (b.win_rate * 100).toFixed(1) + '%' : '—'
}

function avg(b: ReentryBucket): string {
  return b.avg_pnl != null ? b.avg_pnl.toFixed(2) : '—'
}

function pnlColor(v: number | null): string {
  if (v == null) return '#909399'
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>再入场行为</h2>
    <p class="page-desc">同标的前一笔盈利 / 亏损后，下一笔交易的条件表现，检测「报复性交易」倾向（灵感来自 Freqtrade 序列分析）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="回看天数">
          <el-input-number v-model="days" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">前笔盈利后</span>
                <span class="stat-value" :style="{ color: pnlColor(result.after_win.avg_pnl) }">
                  {{ avg(result.after_win) }}
                </span>
                <span class="stat-sub">{{ result.after_win.trades }} 笔 · 胜率 {{ wr(result.after_win) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">前笔亏损后</span>
                <span class="stat-value" :style="{ color: pnlColor(result.after_loss.avg_pnl) }">
                  {{ avg(result.after_loss) }}
                </span>
                <span class="stat-sub">{{ result.after_loss.trades }} 笔 · 胜率 {{ wr(result.after_loss) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">倾斜差值 (盈后-亏后)</span>
                <span class="stat-value" :style="{ color: pnlColor(result.tilt_avg_pnl_diff) }">
                  {{ result.tilt_avg_pnl_diff != null ? result.tilt_avg_pnl_diff.toFixed(2) : '—' }}
                </span>
                <span class="stat-sub">负值 = 亏后表现更好</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">标的首次交易</span>
                <span class="stat-value" :style="{ color: pnlColor(result.first_of_symbol.avg_pnl) }">
                  {{ avg(result.first_of_symbol) }}
                </span>
                <span class="stat-sub">{{ result.first_of_symbol.trades }} 笔</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>分标的对比</template>
          <el-table :data="result.by_symbol" size="small" max-height="480">
            <el-table-column prop="symbol" label="标的" width="120" />
            <el-table-column label="盈后笔数" width="90">
              <template #default="{ row }">{{ row.after_win.trades }}</template>
            </el-table-column>
            <el-table-column label="盈后胜率" width="90">
              <template #default="{ row }">{{ wr(row.after_win) }}</template>
            </el-table-column>
            <el-table-column label="盈后均盈" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.after_win.avg_pnl) }">{{ avg(row.after_win) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="亏后笔数" width="90">
              <template #default="{ row }">{{ row.after_loss.trades }}</template>
            </el-table-column>
            <el-table-column label="亏后胜率" width="90">
              <template #default="{ row }">{{ wr(row.after_loss) }}</template>
            </el-table-column>
            <el-table-column label="亏后均盈" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.after_loss.avg_pnl) }">{{ avg(row.after_loss) }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 20px; font-weight: 600; }
.stat-sub { font-size: 12px; color: #606266; }
</style>
