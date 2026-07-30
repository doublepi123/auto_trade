<script setup lang="ts">
import { ref } from 'vue'
import { getTimePerformance, type TimePerformanceResult } from '../api/timePerformance'

const loading = ref(false)
const result = ref<TimePerformanceResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getTimePerformance({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
    })
  } finally {
    loading.value = false
  }
}

function pnlColor(v: number): string {
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>时段绩效</h2>
    <p class="page-desc">按小时与星期几拆解已实现 PnL，发现时间维度上的优势或劣势（灵感来自 Freqtrade）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert
        v-if="result.error"
        :title="result.error"
        type="warning"
        :closable="false"
        style="margin-top: 16px"
      />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px" v-if="result.highlights.best_hour">
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="highlight-card">
                <span class="hl-label">最佳时段</span>
                <span class="hl-value text-green">{{ result.highlights.best_hour.bucket }}:00</span>
                <span class="hl-sub" :style="{ color: pnlColor(result.highlights.best_hour.total_pnl) }">
                  {{ result.highlights.best_hour.total_pnl }}
                </span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="highlight-card">
                <span class="hl-label">最差时段</span>
                <span class="hl-value text-red">{{ result.highlights.worst_hour?.bucket }}:00</span>
                <span class="hl-sub" :style="{ color: pnlColor(result.highlights.worst_hour?.total_pnl ?? 0) }">
                  {{ result.highlights.worst_hour?.total_pnl }}
                </span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="highlight-card">
                <span class="hl-label">最佳星期</span>
                <span class="hl-value text-green">{{ result.highlights.best_day?.day_name }}</span>
                <span class="hl-sub" :style="{ color: pnlColor(result.highlights.best_day?.total_pnl ?? 0) }">
                  {{ result.highlights.best_day?.total_pnl }}
                </span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="highlight-card">
                <span class="hl-label">最差星期</span>
                <span class="hl-value text-red">{{ result.highlights.worst_day?.day_name }}</span>
                <span class="hl-sub" :style="{ color: pnlColor(result.highlights.worst_day?.total_pnl ?? 0) }">
                  {{ result.highlights.worst_day?.total_pnl }}
                </span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="14">
            <el-card>
              <template #header>按小时 PnL</template>
              <el-table :data="result.by_hour" size="small" max-height="400">
                <el-table-column prop="bucket" label="时段" width="80">
                  <template #default="{ row }">{{ row.bucket }}:00</template>
                </el-table-column>
                <el-table-column prop="trade_count" label="交易数" width="80" />
                <el-table-column prop="total_pnl" label="总 PnL" width="100">
                  <template #default="{ row }">
                    <span :style="{ color: pnlColor(row.total_pnl) }">{{ row.total_pnl }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="avg_pnl" label="平均 PnL" width="100">
                  <template #default="{ row }">
                    <span :style="{ color: pnlColor(row.avg_pnl) }">{{ row.avg_pnl }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="win_rate" label="胜率" width="100">
                  <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
          <el-col :span="10">
            <el-card>
              <template #header>按星期 PnL</template>
              <el-table :data="result.by_day_of_week" size="small">
                <el-table-column prop="day_name" label="星期" width="80" />
                <el-table-column prop="trade_count" label="交易数" width="80" />
                <el-table-column prop="total_pnl" label="总 PnL" width="100">
                  <template #default="{ row }">
                    <span :style="{ color: pnlColor(row.total_pnl) }">{{ row.total_pnl }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="win_rate" label="胜率" width="100">
                  <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
        </el-row>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
.highlight-card { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.hl-label { font-size: 12px; color: #909399; }
.hl-value { font-size: 22px; font-weight: 700; }
.hl-sub { font-size: 14px; font-weight: 600; }
.text-green { color: #67c23a; }
.text-red { color: #f56c6c; }
</style>
