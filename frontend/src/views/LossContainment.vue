<script setup lang="ts">
import { computed, ref } from 'vue'
import { getLossContainmentSummary, type LossContainmentResult } from '../api/lossContainment'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<LossContainmentResult | null>(null)
const days = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getLossContainmentSummary({ days: days.value })
  } finally {
    loading.value = false
  }
}

const maxCount = computed(() => {
  if (!result.value?.histogram?.length) return 0
  return Math.max(...result.value.histogram.map((b) => b.count))
})

function pct(v: number | null): string {
  return v != null ? (v * 100).toFixed(1) + '%' : '—'
}
</script>

<template>
  <div class="page-container">
    <h2>亏损控制分析</h2>
    <p class="page-desc">亏损单的离场原因分布、尾部超限（&gt;2× 中位亏损）与亏损集中度（灵感来自 VectorBT / Freqtrade 止损分析）</p>

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
                <span class="stat-label">中位 / 平均亏损</span>
                <span class="stat-value text-red">{{ result.median_loss.toFixed(2) }}</span>
                <span class="stat-sub">均值 {{ result.mean_loss.toFixed(2) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">最大单笔亏损</span>
                <span class="stat-value text-red">{{ result.worst_loss.toFixed(2) }}</span>
                <span class="stat-sub">{{ result.worst_to_median != null ? result.worst_to_median.toFixed(1) + '× 中位' : '' }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">尾部超限占比</span>
                <span class="stat-value">{{ pct(result.tail_breach_pct) }}</span>
                <span class="stat-sub">&gt;2× 中位亏损 {{ result.tail_breach_count }} 笔</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">Top 3 亏损占比</span>
                <span class="stat-value">{{ pct(result.top3_loss_share) }}</span>
                <span class="stat-sub">总亏损 {{ result.total_loss.toFixed(2) }}</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>亏损幅度分布（相对中位亏损倍数）</template>
          <div class="hist">
            <div v-for="b in result.histogram" :key="b.bucket" class="hist-item" :title="`${b.bucket}: ${b.count}`">
              <span class="hist-count">{{ b.count || '' }}</span>
              <div class="hist-bar" :style="{ height: maxCount > 0 ? (b.count / maxCount) * 100 + '%' : '0%' }" />
              <span class="hist-label">{{ b.bucket }}</span>
            </div>
          </div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>按离场原因</template>
          <el-table :data="result.by_exit_cause" size="small" max-height="480">
            <el-table-column prop="exit_cause" label="离场原因" width="160" />
            <el-table-column prop="count" label="笔数" width="80" />
            <el-table-column prop="total_loss" label="合计亏损" width="110" />
            <el-table-column prop="avg_loss" label="平均亏损" width="110" />
            <el-table-column label="亏损占比">
              <template #default="{ row }">
                <div class="share-wrap">
                  <div
                    class="share-bar"
                    :style="{ width: row.share_of_loss != null ? (row.share_of_loss * 100).toFixed(1) + '%' : '0%' }"
                  />
                  <span>{{ pct(row.share_of_loss) }}</span>
                </div>
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
.text-red { color: #f56c6c; }
.hist { display: flex; align-items: flex-end; gap: 16px; height: 160px; }
.hist-item { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; }
.hist-count { font-size: 11px; color: #606266; }
.hist-bar { width: 60%; background: #f56c6c; border-radius: 2px 2px 0 0; min-height: 1px; }
.hist-label { font-size: 10px; color: #909399; margin-top: 4px; }
.share-wrap { display: flex; align-items: center; gap: 8px; }
.share-bar { height: 12px; background: #f56c6c; border-radius: 2px; min-width: 1px; }
</style>
