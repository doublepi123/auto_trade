<script setup lang="ts">
import { ref } from 'vue'
import { getFirstTradeSummary, type FirstTradeResult, type FirstTradeBucket } from '../api/firstTrade'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<FirstTradeResult | null>(null)
const days = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getFirstTradeSummary({ days: days.value })
  } finally {
    loading.value = false
  }
}

function wr(b: FirstTradeBucket): string {
  return b.win_rate != null ? (b.win_rate * 100).toFixed(1) + '%' : '—'
}

function avg(b: FirstTradeBucket): string {
  return b.avg_pnl != null ? b.avg_pnl.toFixed(2) : '—'
}

function pnlColor(v: number | null): string {
  if (v == null) return '#909399'
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>每日首笔平仓效应</h2>
    <p class="page-desc">比较每日首笔平仓与后续平仓；方向一致率只比较首笔与当日其余平仓合计，不把首笔自身计入后续结果</p>

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
      <StatisticsQualityAlert :quality="result.statistics_quality" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">首笔平仓平均盈亏</span>
                <span class="stat-value" :style="{ color: pnlColor(result.first_trade.avg_pnl) }">
                  {{ avg(result.first_trade) }} {{ result.currency ?? '' }}
                </span>
                <span class="stat-sub">{{ result.first_trade.trades }} 天 · 胜率 {{ wr(result.first_trade) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">其余单平均盈亏</span>
                <span class="stat-value" :style="{ color: pnlColor(result.rest_of_day.avg_pnl) }">
                  {{ avg(result.rest_of_day) }} {{ result.currency ?? '' }}
                </span>
                <span class="stat-sub">{{ result.rest_of_day.trades }} 笔 · 胜率 {{ wr(result.rest_of_day) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">首笔/后续方向一致率</span>
                <span class="stat-value">
                  {{ result.tone_sample_sufficient && result.tone_match_pct != null ? (result.tone_match_pct * 100).toFixed(1) + '%' : '样本不足' }}
                </span>
                <span class="stat-sub">
                  首笔平仓方向 = 当日其余平仓合计方向（有效 {{ result.tone_sample_days }}/{{ result.tone_min_sample_days }} 天）
                </span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">盈利日占比</span>
                <span class="stat-value text-green">
                  {{ result.green_day_pct != null ? (result.green_day_pct * 100).toFixed(1) + '%' : '—' }}
                </span>
                <span class="stat-sub">{{ result.trading_days }} 个交易日 · {{ result.multi_trade_days }} 天多笔</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>首笔平仓 vs 其余平仓</template>
          <el-table :data="[result.first_trade, result.rest_of_day]" size="small">
            <el-table-column label="分组" width="140">
              <template #default="{ $index }">{{ $index === 0 ? '每日首笔平仓' : '当日其余平仓' }}</template>
            </el-table-column>
            <el-table-column prop="trades" label="笔数" width="90" />
            <el-table-column label="胜率" width="100">
              <template #default="{ row }">{{ wr(row) }}</template>
            </el-table-column>
            <el-table-column label="平均盈亏" width="110">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.avg_pnl) }">{{ avg(row) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="合计盈亏">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.total_pnl) }">{{ row.total_pnl.toFixed(2) }}</span>
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
</style>
