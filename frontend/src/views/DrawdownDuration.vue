<script setup lang="ts">
import { ref } from 'vue'
import { getDrawdownDuration, type DrawdownDurationResult } from '../api/drawdownDuration'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<DrawdownDurationResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(365)

async function run() {
  loading.value = true
  try {
    result.value = await getDrawdownDuration({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
    })
  } finally {
    loading.value = false
  }
}

function duration(value: number | null): string {
  return value === null ? '—' : String(value)
}
</script>

<template>
  <div class="page-container">
    <h2>回撤持续期</h2>
    <p class="page-desc">仅统计回看窗口内从局部高水位开始且完成恢复的水下 run；窗口前高水位未知，因此不代表完整历史恢复时长</p>

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
          title="窗口局部口径"
          description="曲线从回看窗口起点的累计净 PnL=0 开始；缺少窗口前权益高水位证据，所有结果仅表示 window-local underwater runs。"
          type="warning"
          show-icon
          :closable="false"
          style="margin-top: 16px"
        />
        <el-alert
          v-if="result.note"
          :title="result.note"
          type="info"
          show-icon
          :closable="false"
          style="margin-top: 12px"
        />

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="4">
            <el-card shadow="hover">
              <el-statistic title="完整恢复 episodes" :value="result.completed_episodes" />
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">当前水下持续（笔）</span>
                <span class="stat-value">{{ result.current_open_duration }}</span>
                <span class="stat-sub">{{ result.is_underwater ? '未恢复 / 右删失' : '当前不在水下' }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">平均恢复持续（笔）</span>
                <span class="stat-value">{{ duration(result.summary.avg) }}</span>
                <span class="stat-sub">仅 completed</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">最长恢复持续（笔）</span>
                <span class="stat-value">{{ duration(result.summary.max) }}</span>
                <span class="stat-sub">仅 completed</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">中位恢复持续（笔）</span>
                <span class="stat-value">{{ duration(result.summary.median) }}</span>
                <span class="stat-sub">仅 completed</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <el-statistic title="窗口局部水下%" :value="result.pct_time_underwater" :precision="1" suffix="%" />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>完整恢复持续期分布</template>
          <el-table v-if="result.histogram.length" :data="result.histogram" size="small">
            <el-table-column prop="duration" label="持续笔数" width="100" />
            <el-table-column prop="count" label="出现次数" width="100" />
            <el-table-column label="分布">
              <template #default="{ row }">
                <el-progress :percentage="Math.min(row.count * 15, 100)" :show-text="false" :stroke-width="12" status="exception" />
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="暂无完整观测且已恢复的 episode" :image-size="72" />
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>完整恢复分位数</template>
          <el-descriptions v-if="result.completed_episodes > 0" :column="4" border size="small">
            <el-descriptions-item label="P25">{{ result.summary.p25 }}</el-descriptions-item>
            <el-descriptions-item label="中位">{{ result.summary.median }}</el-descriptions-item>
            <el-descriptions-item label="P75">{{ result.summary.p75 }}</el-descriptions-item>
            <el-descriptions-item label="最大">{{ result.summary.max }}</el-descriptions-item>
          </el-descriptions>
          <el-empty v-else description="没有可用于恢复分位数的 completed episode" :image-size="72" />
          <p class="method-note">中位数：{{ result.median_method }}；分位数：{{ result.quantile_method }}</p>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>删失与观测口径</template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="首个边界 run 左删失">{{ result.left_censored ? '是（已排除）' : '否' }}</el-descriptions-item>
            <el-descriptions-item label="左删失窗口内持续">{{ result.excluded_left_censored_duration ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="已观测水下交易数">{{ result.observed_underwater_trade_count }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
.stat-custom { display: flex; min-height: 58px; flex-direction: column; align-items: center; justify-content: center; }
.stat-label { color: #909399; font-size: 13px; }
.stat-value { margin-top: 6px; font-size: 20px; font-weight: 600; }
.stat-sub { margin-top: 4px; color: #909399; font-size: 11px; }
.method-note { margin: 12px 0 0; color: #909399; font-size: 12px; }
</style>
