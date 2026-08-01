<script setup lang="ts">
import { ref } from 'vue'
import { getTradeFrequency, type TradeFrequencyResult } from '../api/tradeFrequency'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<TradeFrequencyResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getTradeFrequency({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
    })
  } finally {
    loading.value = false
  }
}

function fmtInterval(s: number, pairCount: number): string {
  if (pairCount === 0) return '不可用'
  if (s < 60) return `${s.toFixed(0)}s`
  if (s < 3600) return `${(s / 60).toFixed(1)}m`
  return `${(s / 3600).toFixed(1)}h`
}
</script>

<template>
  <div class="page-container">
    <h2>交易频率分析</h2>
    <p class="page-desc">检测过度交易模式；间隔仅比较同一标的、同一市场本地交易日内的相邻平仓</p>

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
      <StatisticsQualityAlert :quality="result.statistics_quality" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-alert
          :title="result.assessment"
          :type="result.overtrading_flag ? 'error' : 'success'"
          :closable="false"
          style="margin-top: 16px"
        />

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="总交易数" :value="result.total_trades" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="活跃天数" :value="result.active_days" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="日均交易" :value="result.avg_trades_per_day" :precision="1" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="单日最多" :value="result.max_trades_in_day" />
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <el-statistic title="快速连发" :value="result.rapid_fire_count" />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>间隔与频率详情</template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="同标的日内平均间隔">{{ fmtInterval(result.avg_interval_seconds, result.interval_pair_count) }}</el-descriptions-item>
            <el-descriptions-item label="同标的日内最短间隔">{{ fmtInterval(result.min_interval_seconds, result.interval_pair_count) }}</el-descriptions-item>
            <el-descriptions-item label="有效间隔对">{{ result.interval_pair_count }}</el-descriptions-item>
            <el-descriptions-item label="快速连发占比">{{ result.interval_pair_count > 0 ? `${(result.rapid_fire_pct * 100).toFixed(1)}%` : '不可用' }}</el-descriptions-item>
            <el-descriptions-item label="最密集日期">{{ result.max_day_date }}</el-descriptions-item>
            <el-descriptions-item label="过度交易标记">
              <el-tag :type="result.overtrading_flag ? 'danger' : 'success'" size="small">
                {{ result.overtrading_flag ? '是' : '否' }}
              </el-tag>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>每日交易数分布</template>
          <el-table :data="result.daily_distribution" size="small">
            <el-table-column prop="trades_per_day" label="每日交易数" width="120" />
            <el-table-column prop="day_count" label="天数" width="80" />
            <el-table-column label="分布">
              <template #default="{ row }">
                <el-progress :percentage="Math.min(row.day_count * 8, 100)" :show-text="false" :stroke-width="12" />
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
</style>
