<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import DataState from '../components/DataState.vue'
import { getPrimaryCandidacy } from '../api/primaryCandidacy'
import type { PrimaryCandidacyResponse } from '../types'
import {
  candidacyIncumbentStatusLabel,
  candidacyVerdictLabel,
  candidacyWithheldReasonLabel,
  candidateGateReasonLabel,
  poolGateStatusLabel,
  powerVerdictLabel,
} from '../utils/labels'

const loading = ref(false)
const error = ref('')
const report = ref<PrimaryCandidacyResponse | null>(null)
const includeEntryWindows = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    report.value = await getPrimaryCandidacy({
      include_entry_windows: includeEntryWindows.value,
    })
  } catch (e) {
    report.value = null
    error.value = e instanceof Error ? e.message : '加载主标的候选报告失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(includeEntryWindows, load)

const verdictTone = computed(() => {
  switch (report.value?.verdict) {
    case 'SELECTION_SUPPORTED': return 'success'
    case 'SELECTION_NOT_SUPPORTED_BY_EVIDENCE': return 'warning'
    default: return 'info'
  }
})

const poolGateTone = computed(() => {
  switch (report.value?.pool_gate.status) {
    case 'PASS': return 'success'
    case 'BLOCKED': return 'danger'
    default: return 'info'
  }
})

const powerTone = computed(() => (
  report.value?.power.verdict === 'POWERED' ? 'success' : 'warning'
))

function num(value: number | null, digits = 1): string {
  return value == null ? '—' : value.toFixed(digits)
}

function volume(value: number): string {
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`
  return value.toFixed(0)
}
</script>

<template>
  <div class="page-container">
    <h2>主标的候选</h2>
    <p class="page-desc">
      只读证据面板：边际选择优先于闸门通过者，闸门通过者优先于成本排名。
      本页不切换主标的、不下单、不自动晋级。
    </p>

    <DataState :loading="loading" :error="error" :empty="!report">
      <template v-if="report">
        <el-alert
          class="verdict"
          data-testid="candidacy-verdict"
          :type="verdictTone"
          :closable="false"
          show-icon
          :title="candidacyVerdictLabel(report.verdict)"
        >
          在任 {{ report.incumbent }}（{{ candidacyIncumbentStatusLabel(report.incumbent_status) }}）
          · 自动切换{{ report.switch_enabled ? '已开启' : '未开启' }}
          · 生成于 {{ report.generated_at }}
        </el-alert>

        <el-row :gutter="16" class="picks">
          <el-col :span="8">
            <el-card shadow="hover" class="pick-card" data-testid="pick-edge">
              <template #header><span class="pick-title">边际选择（edge）</span></template>
              <template v-if="report.edge_pick">
                <div class="pick-symbol">{{ report.edge_pick.symbol }}</div>
                <div class="pick-meta">
                  reach {{ num(report.edge_pick.reach_rate_pct) }}%
                  · 闭合 {{ report.edge_pick.closed_trades }} 笔
                  · 趋势 {{ num(report.edge_pick.trend_blocked_pct) }}%
                </div>
                <div class="pick-meta">{{ report.edge_pick.selection_rule }}</div>
              </template>
              <template v-else>
                <div class="pick-symbol muted">证据不支持</div>
                <div class="pick-meta">{{ report.edge_pick_withheld_reason }}</div>
              </template>
            </el-card>
          </el-col>

          <el-col :span="8">
            <el-card shadow="hover" class="pick-card" data-testid="pick-gates-only">
              <template #header><span class="pick-title">仅过闸门</span></template>
              <template v-if="report.gates_only_pick">
                <div class="pick-symbol">{{ report.gates_only_pick.symbol }}</div>
                <div class="pick-meta">
                  通过闸门者 {{ report.gates_only_pick.passing_count }} 个
                  · 持有 {{ report.gates_only_pick.trades_held }} /
                  需要 {{ report.gates_only_pick.required_trades_one_sample }} 笔
                </div>
                <div class="pick-tags">
                  <el-tag size="small" type="warning" effect="dark">非 edge 结论</el-tag>
                  <el-tag
                    v-for="reason in report.gates_only_pick.withheld_from_edge_pick_because"
                    :key="reason"
                    size="small"
                    type="info"
                  >{{ candidacyWithheldReasonLabel(reason) }}</el-tag>
                </div>
              </template>
              <div v-else class="pick-symbol muted">无通过闸门的标的</div>
            </el-card>
          </el-col>

          <el-col :span="8">
            <el-card shadow="hover" class="pick-card" data-testid="pick-tradeability">
              <template #header><span class="pick-title">可交易性</span></template>
              <template v-if="report.tradeability_pick">
                <div class="pick-symbol">{{ report.tradeability_pick.symbol }}</div>
                <div class="pick-meta">
                  价差 {{ num(report.tradeability_pick.relative_spread_bps, 2) }} bps
                  · 成交额 {{ volume(report.tradeability_pick.avg_dollar_volume) }}
                </div>
                <div class="pick-meta">截至 {{ report.tradeability_pick.metrics_as_of }}</div>
                <div class="pick-tags">
                  <el-tag size="small" type="warning" effect="dark">成本选择，非 edge 结论</el-tag>
                </div>
              </template>
              <div v-else class="pick-symbol muted">无可交易性排名</div>
            </el-card>
          </el-col>
        </el-row>

        <el-card class="section" data-testid="pool-gate">
          <template #header>
            <span class="section-title">池级信号闸门</span>
            <el-tag size="small" :type="poolGateTone">
              {{ poolGateStatusLabel(report.pool_gate.status) }}
            </el-tag>
          </template>
          <p class="detail">{{ report.pool_gate.detail }}</p>
          <p class="pick-meta">
            自动切换{{ report.pool_gate.enforced_by_switch ? '会' : '不会' }}强制执行该闸门
          </p>
        </el-card>

        <el-card class="section" data-testid="power-card">
          <template #header>
            <span class="section-title">统计功效</span>
            <el-tag size="small" :type="powerTone">
              {{ powerVerdictLabel(report.power.verdict) }}
            </el-tag>
          </template>
          <div class="power-line">
            需要 {{ report.power.required_one_sample ?? '—' }} 笔 /
            最多持有 {{ report.power.max_trades_held }} 笔（{{ report.power.max_trades_symbol }}）
          </div>
          <p class="pick-meta">
            缺口 {{ num(report.power.shortfall_factor, 1) }}×
            · σ {{ num(report.power.sigma_bps, 2) }} bps（{{ report.power.sigma_observations }} 观测）
            · δ {{ report.power.delta_bps }} bps
            · α {{ report.power.alpha }} · power {{ report.power.power }}
          </p>
          <p class="pick-meta">
            双样本需要 {{ report.power.required_two_sample ?? '—' }} 笔
            · reach 闸门工作点 n={{ report.power.reach_gate_operating_point.n }}
            k≥{{ report.power.reach_gate_operating_point.k_min }}
            （{{ report.power.reach_gate_operating_point.basis }}）
          </p>
        </el-card>

        <el-card class="section">
          <template #header><span class="section-title">候选标的闸门</span></template>
          <el-table :data="report.candidates" size="small" data-testid="candidates-table">
            <el-table-column prop="symbol" label="标的" width="110" />
            <el-table-column label="过闸门" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.passes_all_symbol_gates ? 'success' : 'info'">
                  {{ row.passes_all_symbol_gates ? '是' : '否' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="未过原因">
              <template #default="{ row }">
                <el-tag
                  v-for="reason in row.gate_reasons"
                  :key="reason"
                  size="small"
                  type="info"
                  class="reason-tag"
                >{{ candidateGateReasonLabel(reason) }}</el-tag>
                <span v-if="!row.gate_reasons.length" class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="趋势占比" width="100">
              <template #default="{ row }">{{ num(row.trend_blocked_pct) }}%</template>
            </el-table-column>
            <el-table-column prop="closed_trades" label="闭合交易" width="100" />
            <el-table-column label="reach 率" width="100">
              <template #default="{ row }">{{ num(row.reach_rate_pct) }}%</template>
            </el-table-column>
            <el-table-column prop="trades_held" label="持有笔数" width="100" />
            <el-table-column label="功效占比" width="100">
              <template #default="{ row }">{{ num(row.power_share_pct) }}%</template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card class="section">
          <template #header>
            <span class="section-title">可交易性排名</span>
            <el-tag size="small" type="warning">仅成本口径，不构成 edge 结论</el-tag>
          </template>
          <el-table :data="report.tradeability" size="small" data-testid="tradeability-table">
            <el-table-column prop="rank" label="#" width="60" />
            <el-table-column prop="symbol" label="标的" width="110" />
            <el-table-column prop="market" label="市场" width="80" />
            <el-table-column label="相对价差" width="110">
              <template #default="{ row }">{{ num(row.relative_spread_bps, 2) }} bps</template>
            </el-table-column>
            <el-table-column label="日均成交额" width="130">
              <template #default="{ row }">{{ volume(row.avg_dollar_volume) }}</template>
            </el-table-column>
            <el-table-column label="现价" width="100">
              <template #default="{ row }">{{ num(row.price, 2) }}</template>
            </el-table-column>
            <el-table-column label="合格" width="80">
              <template #default="{ row }">
                <el-tag size="small" :type="row.eligible ? 'success' : 'info'">
                  {{ row.eligible ? '是' : '否' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="说明">
              <template #default="{ row }">
                <span v-if="row.board_lot_uncertain" class="muted">每手股数未知 </span>
                <span>{{ row.reasons.join('、') || '—' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-checkbox
          v-model="includeEntryWindows"
          class="opt-in"
          data-testid="include-entry-windows"
        >包含入场窗口明细（重新拉取）</el-checkbox>
      </template>
    </DataState>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.verdict { margin-bottom: 16px; }
.picks { margin-bottom: 16px; }
.pick-card { height: 100%; }
.pick-title { font-size: 13px; color: #606266; }
.pick-symbol { font-size: 22px; font-weight: 600; margin-bottom: 6px; }
.pick-symbol.muted { font-size: 16px; }
.pick-meta { font-size: 12px; color: #909399; line-height: 1.7; margin: 0; }
.pick-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.section { margin-bottom: 16px; }
.section-title { font-size: 13px; color: #606266; margin-right: 8px; }
.detail { font-size: 13px; color: #303133; margin: 0 0 6px; }
.reason-tag { margin-right: 6px; }
.muted { color: #909399; }
.power-line { font-size: 16px; font-weight: 600; margin-bottom: 6px; }
.opt-in { margin-bottom: 24px; }
</style>
