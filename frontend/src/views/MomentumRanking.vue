<script setup lang="ts">
import { ref } from 'vue'
import { getMomentumRanking, type MomentumRankingResult } from '../api/momentumRanking'

const loading = ref(false)
const result = ref<MomentumRankingResult | null>(null)
const lookbackDays = ref(90)
const minTrades = ref(3)

async function run() {
  loading.value = true
  try {
    result.value = await getMomentumRanking({
      lookback_days: lookbackDays.value,
      min_trades: minTrades.value,
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
    <h2>标的动量排名</h2>
    <p class="page-desc">按累计 PnL 斜率对标的进行横截面动量排名（灵感来自 QuantConnect / Lean）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item label="最少交易数">
          <el-input-number v-model="minTrades" :min="1" :max="50" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">排名</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="达标标的" :value="result.qualifying_symbols" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover" v-if="result.top_momentum">
              <div class="stat-custom">
                <span class="stat-label">最强动量</span>
                <span class="stat-value text-green">{{ result.top_momentum.symbol }}</span>
                <span class="stat-sub">slope {{ result.top_momentum.momentum_slope }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover" v-if="result.bottom_momentum">
              <div class="stat-custom">
                <span class="stat-label">最弱动量</span>
                <span class="stat-value text-red">{{ result.bottom_momentum.symbol }}</span>
                <span class="stat-sub">slope {{ result.bottom_momentum.momentum_slope }}</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>动量排名表</template>
          <el-table :data="result.rankings" size="small" max-height="500">
            <el-table-column prop="rank" label="#" width="50" />
            <el-table-column prop="symbol" label="标的" width="120" />
            <el-table-column prop="momentum_slope" label="动量斜率" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.momentum_slope), fontWeight: 700 }">{{ row.momentum_slope }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column prop="total_pnl" label="总 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.total_pnl) }">{{ row.total_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="win_rate" label="胜率" width="80">
              <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
            </el-table-column>
            <el-table-column prop="acceleration" label="加速度" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.acceleration) }">{{ row.acceleration }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="recent_pnl" label="近期 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.recent_pnl) }">{{ row.recent_pnl }}</span>
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
.text-green { color: #67c23a; }
.text-red { color: #f56c6c; }
</style>
