<template>
  <div class="backtest-page" data-testid="backtest-page">
    <div class="page-heading">
      <div>
        <h3>回测</h3>
        <p>{{ form.symbol || '未配置标的' }} · {{ form.short_selling ? '允许做空' : '仅做多' }}</p>
      </div>
      <div class="heading-tags">
        <el-tag effect="plain">{{ result ? `${result.metrics.trade_count} 笔信号` : '待运行' }}</el-tag>
        <el-tag :type="result ? metricTagType(result.metrics.total_pnl) : 'info'" effect="plain">
          {{ result ? signedCurrency(result.metrics.total_pnl) : '无结果' }}
        </el-tag>
      </div>
    </div>

    <section class="backtest-tool">
      <div class="tool-panel params-panel">
        <div class="panel-heading">
          <h4>参数</h4>
          <el-button size="small" plain @click="loadCurrentStrategy">同步当前策略</el-button>
        </div>
        <el-form label-width="132px" @submit.prevent="handleRun">
          <el-form-item label="股票代码">
            <el-input v-model="form.symbol" placeholder="AAPL.US" />
          </el-form-item>
          <div class="form-grid">
            <el-form-item label="买入价下限">
              <el-input-number v-model="form.buy_low" :precision="2" :step="0.01" :min="0.01" />
            </el-form-item>
            <el-form-item label="卖出价上限">
              <el-input-number v-model="form.sell_high" :precision="2" :step="0.01" :min="0.01" />
            </el-form-item>
            <el-form-item label="数量">
              <el-input-number v-model="form.quantity" :precision="0" :step="1" :min="1" />
            </el-form-item>
            <el-form-item label="初始资金">
              <el-input-number v-model="form.initial_cash" :precision="2" :step="1000" :min="1" />
            </el-form-item>
            <el-form-item label="最低盈利">
              <el-input-number v-model="form.min_profit_amount" :precision="2" :step="0.01" :min="0" />
            </el-form-item>
            <el-form-item label="止损百分比">
              <el-input-number v-model="form.stop_loss_pct" :precision="2" :step="0.5" :min="0" :max="100" />
            </el-form-item>
            <el-form-item label="单日最大亏损">
              <el-input-number v-model="form.max_daily_loss" :precision="2" :step="100" :min="1" />
            </el-form-item>
            <el-form-item label="连续亏损阈值">
              <el-input-number v-model="form.max_consecutive_losses" :precision="0" :step="1" :min="1" />
            </el-form-item>
            <el-form-item label="费率">
              <el-input-number v-model="form.fee_rate" :precision="5" :step="0.0005" :min="0" :max="0.1" />
            </el-form-item>
            <el-form-item label="固定费用">
              <el-input-number v-model="form.fixed_fee" :precision="2" :step="0.1" :min="0" />
            </el-form-item>
            <el-form-item label="滑点百分比">
              <el-input-number v-model="form.slippage_pct" :precision="3" :step="0.05" :min="0" :max="5" />
            </el-form-item>
            <el-form-item label="做空">
              <el-switch v-model="form.short_selling" />
            </el-form-item>
          </div>
        </el-form>
      </div>

      <div class="tool-panel data-panel">
        <div class="panel-heading">
          <h4>历史数据</h4>
          <div class="panel-actions">
            <input ref="fileInput" class="file-input" type="file" accept=".csv,text/csv" @change="handleFileUpload" />
            <el-button size="small" plain @click="fileInput?.click()">上传 CSV</el-button>
            <el-button size="small" plain @click="loadSampleCsv">载入示例</el-button>
          </div>
        </div>
        <div class="broker-fetch">
          <span class="broker-fetch-label">从行情拉取：</span>
          <el-input v-model="candleSymbol" placeholder="AAPL.US" style="width: 130px" data-testid="candle-symbol" />
          <el-select v-model="candlePeriod" style="width: 110px" data-testid="candle-period">
            <el-option label="日 K" value="DAY" />
            <el-option label="周 K" value="WEEK" />
            <el-option label="1 分" value="MIN_1" />
            <el-option label="5 分" value="MIN_5" />
            <el-option label="15 分" value="MIN_15" />
            <el-option label="30 分" value="MIN_30" />
            <el-option label="60 分" value="MIN_60" />
          </el-select>
          <el-input-number v-model="candleCount" :min="1" :max="1000" :step="10" style="width: 120px" data-testid="candle-count" />
          <el-button size="small" type="primary" plain :loading="candleLoading" :disabled="!candleSymbol.trim()" data-testid="candle-fetch" @click="fetchCandles">拉取并填入</el-button>
        </div>
        <el-input
          v-model="csvText"
          type="textarea"
          :rows="13"
          resize="vertical"
          data-testid="backtest-csv-input"
          placeholder="timestamp,open,high,low,close,volume"
        />
        <div class="run-row">
          <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
          <el-button
            type="primary"
            :loading="running"
            :disabled="!canRun"
            data-testid="run-backtest-button"
            @click="handleRun"
          >
            运行回测
          </el-button>
        </div>
      </div>
    </section>

    <section class="sweep-section">
      <el-collapse v-model="sweepOpen">
        <el-collapse-item name="sweep" data-testid="sweep-panel">
          <template #title>
            <span class="sweep-title">参数扫描 (Parameter Sweep)</span>
            <el-tag v-if="sweepResult" size="small" effect="plain" class="sweep-title-tag">
              {{ sweepResult.evaluated_count }} 组 · 最优 {{ metricLabel(sweepResult.sort_by) }}
            </el-tag>
          </template>

          <p class="sweep-intro">
            在当前 CSV 上对 buy_low / sell_high / min_profit 做网格搜索，按风险调整收益排名。
            <strong>即时、离线</strong>，与「实验」页的保存式批量回测不同。
          </p>

          <div class="sweep-grid-form">
            <div class="sweep-axis">
              <label>买入价下限 (起 / 步 / 止)</label>
              <div class="triple">
                <el-input-number v-model="sweepForm.buyLowRange.start" :precision="2" :step="1" :min="0.01" data-testid="sweep-buy-low-start" />
                <el-input-number v-model="sweepForm.buyLowRange.step" :precision="2" :step="1" :min="0.01" />
                <el-input-number v-model="sweepForm.buyLowRange.end" :precision="2" :step="1" :min="0.01" />
              </div>
            </div>
            <div class="sweep-axis">
              <label>卖出价上限 (起 / 步 / 止)</label>
              <div class="triple">
                <el-input-number v-model="sweepForm.sellHighRange.start" :precision="2" :step="1" :min="0.01" data-testid="sweep-sell-high-start" />
                <el-input-number v-model="sweepForm.sellHighRange.step" :precision="2" :step="1" :min="0.01" />
                <el-input-number v-model="sweepForm.sellHighRange.end" :precision="2" :step="1" :min="0.01" />
              </div>
            </div>
            <div class="sweep-axis">
              <label>最低盈利候选 (逗号分隔)</label>
              <el-input v-model="sweepForm.minProfitValuesText" placeholder="0, 5, 10" data-testid="sweep-min-profit" />
            </div>
            <div class="sweep-axis">
              <label>排序指标</label>
              <el-select v-model="sweepForm.sortBy" data-testid="sweep-sort-by">
                <el-option v-for="opt in sortByOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
              </el-select>
            </div>
          </div>

          <div class="run-row">
            <el-alert v-if="sweepError" :title="sweepError" type="error" show-icon :closable="false" />
            <el-button size="small" plain @click="seedSweepFromForm">从当前参数生成</el-button>
            <el-button
              type="primary"
              :loading="sweepRunning"
              :disabled="!canSweep"
              data-testid="run-sweep-button"
              @click="handleRunSweep"
            >
              运行扫描
            </el-button>
          </div>

          <p v-if="sweepResult && sweepResult.skipped_count > 0" class="sweep-note">
            已评估 {{ sweepResult.evaluated_count }} 组，跳过 {{ sweepResult.skipped_count }} 组无效组合（buy_low ≥ sell_high）。
          </p>

          <div v-if="sweepResult" class="sweep-results">
            <div class="result-panel" data-testid="sweep-results-table">
              <div class="section-title">
                <h4>排名（Top {{ sweepTopRows.length }}）</h4>
                <span>按 {{ metricLabel(sweepResult.sort_by) }} 降序</span>
              </div>
              <el-table :data="sweepTopRows" size="small" class="responsive-table sweep-table" @row-click="applySweepRow">
                <el-table-column prop="rank" label="#" width="48" />
                <el-table-column label="buy_low" min-width="86">
                  <template #default="{ row }">{{ row.params.buy_low.toFixed(2) }}</template>
                </el-table-column>
                <el-table-column label="sell_high" min-width="86">
                  <template #default="{ row }">{{ row.params.sell_high.toFixed(2) }}</template>
                </el-table-column>
                <el-table-column label="min_profit" min-width="96">
                  <template #default="{ row }">{{ row.params.min_profit_amount.toFixed(2) }}</template>
                </el-table-column>
                <el-table-column label="总收益" min-width="96">
                  <template #default="{ row }">
                    <span :class="metricClass(row.metrics.total_pnl)">{{ signedCurrency(row.metrics.total_pnl) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="回报%" min-width="86">
                  <template #default="{ row }">{{ signedPercent(row.metrics.total_return_pct) }}</template>
                </el-table-column>
                <el-table-column label="回撤%" min-width="76">
                  <template #default="{ row }">{{ row.metrics.max_drawdown_pct.toFixed(2) }}%</template>
                </el-table-column>
                <el-table-column :label="metricLabel(sweepResult.sort_by)" min-width="86">
                  <template #default="{ row }">{{ formatMetric(row.metrics, sweepResult.sort_by) }}</template>
                </el-table-column>
                <el-table-column label="胜率%" min-width="76">
                  <template #default="{ row }">{{ row.metrics.win_rate.toFixed(1) }}</template>
                </el-table-column>
              </el-table>
              <p class="sweep-hint">点击任一行可将该参数应用到上方表单，再运行完整回测查看曲线。</p>
            </div>

            <div class="result-panel" data-testid="sweep-heatmap">
              <div class="section-title">
                <h4>热力图（{{ metricLabel(sweepResult.sort_by) }}）</h4>
                <span>每格取最优 min_profit</span>
              </div>
              <div v-if="heatmapCols.length > 1" class="heatmap-wrap">
                <table class="heatmap-table">
                  <thead>
                    <tr>
                      <th>buy_low ＼ sell_high</th>
                      <th v-for="c in heatmapCols" :key="c">{{ c.toFixed(2) }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="r in heatmapRows" :key="r">
                      <th>{{ r.toFixed(2) }}</th>
                      <td v-for="c in heatmapCols" :key="c" :style="heatmapCellStyle(r, c)">
                        {{ formatHeatmapCell(r, c) }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p v-else class="empty-note">需要至少扫描 buy_low 与 sell_high 两个轴以生成热力图。</p>
            </div>
          </div>
        </el-collapse-item>

        <el-collapse-item name="walkforward" data-testid="walkforward-panel">
          <template #title>
            <span class="sweep-title">Walk-Forward 滚动窗口（样本外稳定性）</span>
            <el-tag v-if="wfResult" size="small" effect="plain" class="sweep-title-tag">
              {{ wfResult.summary.window_count }} 窗口
            </el-tag>
          </template>

          <p class="sweep-intro">
            把数据切成连续「训练 / 测试」窗口：每个训练窗口用上方扫描网格寻优，再在紧随其后的测试窗口（样本外）评估。
            <strong>用于检测过拟合</strong>——全段好看但跨窗口回报方差大 / 盈利窗口占比低，说明配置脆弱。
          </p>

          <div class="sweep-grid-form">
            <div class="sweep-axis">
              <label>训练窗口 (bars)</label>
              <el-input-number v-model="wfForm.trainSize" :min="2" :step="1" data-testid="wf-train" />
            </div>
            <div class="sweep-axis">
              <label>测试窗口 (bars)</label>
              <el-input-number v-model="wfForm.testSize" :min="1" :step="1" data-testid="wf-test" />
            </div>
            <div class="sweep-axis">
              <label>步长 (0=不重叠)</label>
              <el-input-number v-model="wfForm.step" :min="0" :step="1" data-testid="wf-step" />
            </div>
            <div class="sweep-axis">
              <label>寻优指标</label>
              <el-select v-model="wfForm.sortBy" data-testid="wf-sort-by">
                <el-option v-for="opt in sortByOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
              </el-select>
            </div>
            <div class="sweep-axis">
              <label>每窗口寻优</label>
              <el-switch v-model="wfForm.optimize" data-testid="wf-optimize" />
              <small class="sweep-hint">{{ wfForm.optimize ? '用扫描网格寻优（较慢）' : '仅滚动评估当前参数' }}</small>
            </div>
          </div>

          <div class="run-row">
            <el-alert v-if="wfError" :title="wfError" type="error" show-icon :closable="false" />
            <el-button
              type="primary"
              :loading="wfRunning"
              :disabled="!canWalkForward"
              data-testid="run-walkforward-button"
              @click="handleRunWalkForward"
            >
              运行 Walk-Forward
            </el-button>
          </div>

          <div v-if="wfResult" class="wf-results">
            <div class="metrics-grid wf-summary">
              <div class="metric-item">
                <span>窗口数</span>
                <strong>{{ wfResult.summary.window_count }}</strong>
                <small>已评估 {{ wfResult.summary.evaluated_window_count }}</small>
              </div>
              <div class="metric-item">
                <span>样本外均值回报</span>
                <strong :class="metricClass(wfResult.summary.mean_test_return_pct ?? 0)">{{ fmtPct(wfResult.summary.mean_test_return_pct) }}</strong>
                <small>中位 {{ fmtPct(wfResult.summary.median_test_return_pct) }}</small>
              </div>
              <div class="metric-item">
                <span>均值 {{ metricLabel(wfResult.sort_by) }}</span>
                <strong>{{ fmt(wfResult.summary.mean_test_metric) }}</strong>
                <small>样本外</small>
              </div>
              <div class="metric-item">
                <span>盈利窗口占比</span>
                <strong>{{ wfResult.summary.profitable_window_pct === null ? '—' : wfResult.summary.profitable_window_pct.toFixed(0) + '%' }}</strong>
                <small>越高越稳健</small>
              </div>
              <div class="metric-item">
                <span>回报标准差</span>
                <strong>{{ fmt(wfResult.summary.test_return_std_pct) }}%</strong>
                <small>越低越稳定</small>
              </div>
              <div class="metric-item">
                <span>训练 / 测试</span>
                <strong>{{ wfResult.train_size }} / {{ wfResult.test_size }}</strong>
                <small>步长 {{ wfResult.step }}</small>
              </div>
            </div>

            <div class="result-panel" data-testid="wf-windows-table">
              <div class="section-title">
                <h4>逐窗口样本外表现</h4>
                <span>{{ wfResult.windows.length }} 个</span>
              </div>
              <el-table :data="wfResult.windows" size="small" class="responsive-table">
                <el-table-column prop="index" label="#" width="48" />
                <el-table-column label="区间" min-width="220">
                  <template #default="{ row }">{{ formatDateTime(row.start) }} → {{ formatDateTime(row.end) }}</template>
                </el-table-column>
                <el-table-column label="最优 buy_low" min-width="100">
                  <template #default="{ row }">{{ row.best_params ? row.best_params.buy_low.toFixed(2) : '—' }}</template>
                </el-table-column>
                <el-table-column label="样本外回报%" min-width="110">
                  <template #default="{ row }">
                    <span :class="metricClass(row.test_metrics?.total_return_pct ?? 0)">{{ fmtPct(row.test_metrics?.total_return_pct) }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="metricLabel(wfResult.sort_by)" min-width="90">
                  <template #default="{ row }">{{ row.test_metrics ? formatMetric(row.test_metrics, wfResult.sort_by) : '—' }}</template>
                </el-table-column>
                <el-table-column label="样本外 PnL" min-width="100">
                  <template #default="{ row }">
                    <span :class="metricClass(row.test_metrics?.total_pnl ?? 0)">{{ signedCurrency(row.test_metrics?.total_pnl) }}</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </div>
        </el-collapse-item>

        <el-collapse-item name="stress" data-testid="stress-panel">
          <template #title>
            <span class="sweep-title">What-If 压力测试（路径敏感性）</span>
            <el-tag v-if="stressResult" size="small" effect="plain" class="sweep-title-tag">
              {{ stressResult.scenarios_run }} 场景
            </el-tag>
          </template>

          <p class="sweep-intro">
            对当前 CSV 做<strong>蒙特卡洛扰动</strong>：把每根 K 线 OHLC 按设定幅度随机缩放后重跑，得到收益分布。
            回答「若实际价格路径略不同于历史，结果会多差？」——P5 / 最差 / 盈利场景占比越稳健越好。确定性（固定 seed）。
          </p>

          <div class="sweep-grid-form">
            <div class="sweep-axis">
              <label>场景数</label>
              <el-input-number v-model="stressForm.scenarios" :min="1" :max="1000" :step="10" data-testid="stress-scenarios" />
            </div>
            <div class="sweep-axis">
              <label>价格扰动 %</label>
              <el-input-number v-model="stressForm.jitterPct" :min="0" :max="20" :step="0.5" :precision="2" data-testid="stress-jitter" />
            </div>
            <div class="sweep-axis">
              <label>随机种子</label>
              <el-input-number v-model="stressForm.seed" :min="0" :step="1" data-testid="stress-seed" />
            </div>
          </div>

          <div class="run-row">
            <el-alert v-if="stressError" :title="stressError" type="error" show-icon :closable="false" />
            <el-button type="primary" :loading="stressRunning" :disabled="!canStress" data-testid="run-stress-button" @click="handleRunStress">
              运行压力测试
            </el-button>
          </div>

          <div v-if="stressResult" class="metrics-grid wf-summary" data-testid="stress-summary">
            <div class="metric-item">
              <span>基线回报</span>
              <strong :class="metricClass(stressResult.baseline_return_pct ?? 0)">{{ fmtPct(stressResult.baseline_return_pct) }}</strong>
              <small>未扰动</small>
            </div>
            <div class="metric-item">
              <span>中位回报</span>
              <strong :class="metricClass(stressResult.median_return_pct ?? 0)">{{ fmtPct(stressResult.median_return_pct) }}</strong>
              <small>P50</small>
            </div>
            <div class="metric-item">
              <span>P5 回报</span>
              <strong :class="metricClass(stressResult.p5_return_pct ?? 0)">{{ fmtPct(stressResult.p5_return_pct) }}</strong>
              <small>悲观</small>
            </div>
            <div class="metric-item">
              <span>P95 回报</span>
              <strong :class="metricClass(stressResult.p95_return_pct ?? 0)">{{ fmtPct(stressResult.p95_return_pct) }}</strong>
              <small>乐观</small>
            </div>
            <div class="metric-item">
              <span>最差回报</span>
              <strong :class="metricClass(stressResult.worst_return_pct ?? 0)">{{ fmtPct(stressResult.worst_return_pct) }}</strong>
              <small>最大回撤 {{ fmt(stressResult.worst_drawdown_pct) }}%</small>
            </div>
            <div class="metric-item">
              <span>盈利场景占比</span>
              <strong>{{ stressResult.profitable_scenario_pct === null ? '—' : stressResult.profitable_scenario_pct.toFixed(0) + '%' }}</strong>
              <small>越稳健越好</small>
            </div>
          </div>
        </el-collapse-item>

        <el-collapse-item name="compare" data-testid="compare-panel">
          <template #title>
            <span class="sweep-title">结果对比（保存的回测）</span>
            <el-tag v-if="savedRuns.length" size="small" effect="plain" class="sweep-title-tag">{{ savedRuns.length }} 条</el-tag>
          </template>

          <p class="sweep-intro">
            把当前回测结果存为命名快照，多选后在转置表里横向对比指标。仅存 params + metrics（不存 equity 曲线）。
          </p>

          <div v-if="result" class="run-row" style="justify-content: flex-start">
            <el-input v-model="saveName" placeholder="为当前结果命名" style="max-width: 220px" data-testid="compare-save-name" />
            <el-button type="primary" plain :loading="savingRun" :disabled="!saveName.trim()" data-testid="compare-save-button" @click="handleSaveRun">保存当前结果</el-button>
            <el-button plain @click="loadSavedRuns">刷新列表</el-button>
          </div>

          <el-table v-if="savedRuns.length" :data="savedRuns" size="small" class="responsive-table" @selection-change="onSelectionChange">
            <el-table-column type="selection" width="42" />
            <el-table-column prop="name" label="名称" min-width="120" />
            <el-table-column prop="symbol" label="标的" min-width="90" />
            <el-table-column label="总收益" min-width="90">
              <template #default="{ row }"><span :class="metricClass(row.metrics.total_pnl)">{{ signedCurrency(row.metrics.total_pnl) }}</span></template>
            </el-table-column>
            <el-table-column label="回报%" min-width="80">
              <template #default="{ row }">{{ signedPercent(row.metrics.total_return_pct) }}</template>
            </el-table-column>
            <el-table-column label="Sharpe" min-width="76">
              <template #default="{ row }">{{ fmt(row.metrics.sharpe_ratio) }}</template>
            </el-table-column>
            <el-table-column label="时间" min-width="150">
              <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="" width="70">
              <template #default="{ row }">
                <el-button link type="danger" size="small" @click="handleDeleteRun(row.id)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <p v-else class="empty-note">暂无保存的回测。运行一次回测后点「保存当前结果」。</p>

          <div v-if="savedRuns.length" class="run-row" style="justify-content: flex-start; margin-top: 8px">
            <el-button type="primary" :disabled="selectedRunIds.length < 2" data-testid="compare-run-button" @click="handleCompare">对比选中（{{ selectedRunIds.length }}）</el-button>
          </div>

          <div v-if="compareRuns.length" class="result-panel" data-testid="compare-table">
            <div class="section-title"><h4>横向对比</h4></div>
            <table class="heatmap-table compare-table">
              <thead>
                <tr>
                  <th>指标</th>
                  <th v-for="r in compareRuns" :key="r.id">{{ r.name }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="metric in compareMetricRows" :key="metric.key">
                  <th>{{ metric.label }}</th>
                  <td v-for="r in compareRuns" :key="r.id">{{ metric.format(r) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </el-collapse-item>
      </el-collapse>
    </section>

    <section v-if="result" class="metrics-grid" data-testid="backtest-metrics">
      <div class="metric-item">
        <span>总收益</span>
        <strong :class="metricClass(result.metrics.total_pnl)">{{ signedCurrency(result.metrics.total_pnl) }}</strong>
        <small>{{ signedPercent(result.metrics.total_return_pct) }}</small>
      </div>
      <div class="metric-item">
        <span>胜率</span>
        <strong>{{ result.metrics.win_rate.toFixed(2) }}%</strong>
        <small>{{ result.metrics.winning_trades }} 胜 / {{ result.metrics.losing_trades }} 负</small>
      </div>
      <div class="metric-item">
        <span>最大回撤</span>
        <strong>{{ result.metrics.max_drawdown_pct.toFixed(2) }}%</strong>
        <small>权益低点压力</small>
      </div>
      <div class="metric-item">
        <span>平均持仓</span>
        <strong>{{ result.metrics.avg_holding_minutes.toFixed(1) }}m</strong>
        <small>{{ result.metrics.closed_trade_count }} 笔闭环</small>
      </div>
      <div class="metric-item">
        <span>费用</span>
        <strong>{{ formatCurrency(result.metrics.fees_paid, marketFromSymbol(form.symbol)) }}</strong>
        <small>{{ result.fee_sensitivity.length }} 档敏感性</small>
      </div>
      <div class="metric-item">
        <span>最终状态</span>
        <strong>{{ stateLabel(result.metrics.final_state) }}</strong>
        <small>{{ result.metrics.skipped_signals }} 个跳过信号</small>
      </div>
    </section>

    <BacktestChart v-if="result" :result="result" />

    <section v-if="result" class="result-grid">
      <div class="result-panel" data-testid="backtest-trades">
        <div class="section-title">
          <h4>交易明细</h4>
          <span>{{ result.trades.length }} 条</span>
        </div>
        <el-table :data="result.trades" size="small" class="responsive-table">
          <el-table-column prop="timestamp" label="时间" min-width="160">
            <template #default="{ row }">{{ formatDateTime(row.timestamp) }}</template>
          </el-table-column>
          <el-table-column prop="action" label="动作" min-width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="actionTagType(row.action)" effect="plain">{{ actionLabel(row.action) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="price" label="价格" min-width="100">
            <template #default="{ row }">{{ formatCurrency(row.price, marketFromSymbol(form.symbol)) }}</template>
          </el-table-column>
          <el-table-column prop="quantity" label="数量" min-width="80">
            <template #default="{ row }">{{ row.quantity.toFixed(0) }}</template>
          </el-table-column>
          <el-table-column prop="fee" label="费用" min-width="90">
            <template #default="{ row }">{{ formatCurrency(row.fee, marketFromSymbol(form.symbol)) }}</template>
          </el-table-column>
          <el-table-column prop="pnl" label="盈亏" min-width="100">
            <template #default="{ row }">
              <span :class="metricClass(row.pnl)">{{ signedCurrency(row.pnl) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="result-panel">
        <div class="section-title">
          <h4>跳过原因</h4>
          <span>{{ result.skipped_signals.length }} 条</span>
        </div>
        <div v-if="result.skipped_signals.length > 0" class="skip-list">
          <div v-for="signal in result.skipped_signals" :key="`${signal.timestamp}-${signal.action}`" class="skip-row">
            <strong>{{ actionLabel(signal.action) }} · {{ formatCurrency(signal.price, marketFromSymbol(form.symbol)) }}</strong>
            <span>{{ formatDateTime(signal.timestamp) }}</span>
            <el-tag v-if="signal.category" size="small" type="warning" effect="plain">
              {{ skipCategoryLabel(signal.category) }}
            </el-tag>
            <p>{{ signal.reason }}</p>
          </div>
        </div>
        <p v-else class="empty-note">暂无跳过信号</p>

        <h4 class="subsection-title">费用敏感性</h4>
        <el-table :data="result.fee_sensitivity" size="small" class="responsive-table">
          <el-table-column prop="fee_rate" label="费率" min-width="90">
            <template #default="{ row }">{{ (row.fee_rate * 100).toFixed(3) }}%</template>
          </el-table-column>
          <el-table-column prop="total_pnl" label="总收益" min-width="100">
            <template #default="{ row }">
              <span :class="metricClass(row.total_pnl)">{{ signedCurrency(row.total_pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="max_drawdown_pct" label="回撤" min-width="80">
            <template #default="{ row }">{{ row.max_drawdown_pct.toFixed(2) }}%</template>
          </el-table-column>
        </el-table>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, type CSSProperties } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import BacktestChart from '../components/BacktestChart.vue'
import { getStrategy, runBacktest, runBacktestSweep, runWalkForward, runStressTest, saveBacktestRun, listBacktestRuns, compareBacktestRuns, deleteBacktestRun, getBrokerCandles } from '../api'
import type {
  BacktestMetrics,
  BacktestParams,
  BacktestResult,
  BacktestSweepGrid,
  BacktestSweepHeatmapCell,
  BacktestSweepResult,
  BacktestSweepRow,
  BacktestSweepSortBy,
  WalkForwardResult,
  StressTestResult,
  BacktestRunOut,
} from '../types'
import { skipCategoryLabel } from '../utils/labels'
import { formatCurrency, marketFromSymbol } from '../utils/format'
import { resolveErrorMessage } from '../utils/error'

const defaultParams: BacktestParams = {
  symbol: '',
  buy_low: 100,
  sell_high: 200,
  short_selling: false,
  min_profit_amount: 0,
  max_daily_loss: 5000,
  max_consecutive_losses: 3,
  quantity: 1,
  initial_cash: 100000,
  fee_rate: 0,
  fixed_fee: 0,
  slippage_pct: 0,
  stop_loss_pct: 0,
}

// Snapshot used by loadCurrentStrategy() to detect whether the user has
// modified any of the strategy-mirrored fields since the page was opened.
// Compared field-by-field so a re-sync of the same values is not flagged.
const initialForm: BacktestParams = { ...defaultParams }

const sampleCsv = `timestamp,open,high,low,close,volume
2026-05-22T10:00:00Z,150,160,99,105,1000
2026-05-22T10:01:00Z,120,140,110,130,1200
2026-05-22T10:02:00Z,150,201,145,200,1300
2026-05-22T10:03:00Z,180,190,120,130,900
2026-05-22T10:04:00Z,110,150,98,102,1100
2026-05-22T10:05:00Z,150,205,140,202,1250`

const form = ref<BacktestParams>({ ...defaultParams })
const csvText = ref(sampleCsv)
const result = ref<BacktestResult | null>(null)
const running = ref(false)
const error = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

const canRun = computed(() => (
  csvText.value.trim().length > 0
  && form.value.buy_low > 0
  && form.value.sell_high > form.value.buy_low
  && form.value.quantity > 0
  && form.value.initial_cash > 0
))

async function loadCurrentStrategy() {
  // Detect unsaved edits to avoid silently clobbering the user's input.
  const baseline = initialForm
  const isDirty = (
    form.value.symbol !== baseline.symbol
    || form.value.buy_low !== baseline.buy_low
    || form.value.sell_high !== baseline.sell_high
    || form.value.short_selling !== baseline.short_selling
    || form.value.min_profit_amount !== baseline.min_profit_amount
    || form.value.max_daily_loss !== baseline.max_daily_loss
    || form.value.max_consecutive_losses !== baseline.max_consecutive_losses
  )
  if (isDirty) {
    try {
      await ElMessageBox.confirm(
        '当前表单有未保存的编辑，同步策略将覆盖这些修改。是否继续？',
        '同步当前策略',
        { confirmButtonText: '覆盖并同步', cancelButtonText: '取消', type: 'warning' }
      )
    } catch {
      return
    }
  }
  try {
    const strategy = await getStrategy()
    form.value = {
      ...form.value,
      symbol: strategy.symbol,
      buy_low: strategy.buy_low > 0 ? strategy.buy_low : form.value.buy_low,
      sell_high: strategy.sell_high > strategy.buy_low ? strategy.sell_high : form.value.sell_high,
      short_selling: strategy.short_selling,
      min_profit_amount: strategy.min_profit_amount,
      max_daily_loss: strategy.max_daily_loss,
      max_consecutive_losses: strategy.max_consecutive_losses,
    }
    // Refresh the baseline so the next "sync" doesn't re-flag the values
    // we just pulled from the live strategy. Without this, the second
    // click of the button always asks "覆盖并同步" even when nothing has
    // actually changed.
    initialForm.symbol = form.value.symbol
    initialForm.buy_low = form.value.buy_low
    initialForm.sell_high = form.value.sell_high
    initialForm.short_selling = form.value.short_selling
    initialForm.min_profit_amount = form.value.min_profit_amount
    initialForm.max_daily_loss = form.value.max_daily_loss
    initialForm.max_consecutive_losses = form.value.max_consecutive_losses
    ElMessage.success('已同步当前策略')
  } catch {
    ElMessage.error('同步失败')
  }
}

function loadSampleCsv() {
  csvText.value = sampleCsv
  error.value = ''
}

// ---- Broker candlesticks → backtest ----
const candleSymbol = ref('')
const candlePeriod = ref('DAY')
const candleCount = ref(60)
const candleLoading = ref(false)

async function fetchCandles() {
  if (!candleSymbol.value.trim()) return
  candleLoading.value = true
  error.value = ''
  try {
    const res = await getBrokerCandles(candleSymbol.value.trim().toUpperCase(), candlePeriod.value, candleCount.value)
    if (!res.csv_text) {
      error.value = '未拉取到有效 K 线（券商可能未连接或返回为空）'
      return
    }
    csvText.value = res.csv_text
    ElMessage.success(`已拉取 ${res.count} 根 ${res.period} K 线`)
  } catch (e) {
    error.value = resolveErrorMessage(e, '拉取行情失败（券商可能未连接）')
  } finally {
    candleLoading.value = false
  }
}

async function handleFileUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 10 * 1024 * 1024) {
    ElMessage.error('CSV 文件不能超过 10 MB')
    return
  }
  csvText.value = await file.text()
  input.value = ''
}

async function handleRun() {
  if (!canRun.value) return
  running.value = true
  error.value = ''
  try {
    result.value = await runBacktest({
      params: {
        ...form.value,
        symbol: form.value.symbol.trim().toUpperCase(),
      },
      csv_text: csvText.value,
    })
    ElMessage.success('回测完成')
  } catch (err) {
    error.value = resolveErrorMessage(err, '回测请求失败')
  } finally {
    running.value = false
  }
}

function formatNumber(value: number | null | undefined): string {
  return (value ?? 0).toFixed(2)
}

// ---------------------------------------------------------------------------
// Parameter Sweep — grid-search buy_low / sell_high / min_profit over the
// current CSV and rank combinations by a risk-adjusted metric. Instant +
// offline (no live order path); distinct from the saved, async Experiments.
// ---------------------------------------------------------------------------

const SWEEP_TOP_N = 20

const sortByOptions: { value: BacktestSweepSortBy; label: string }[] = [
  { value: 'sharpe_ratio', label: 'Sharpe' },
  { value: 'sortino_ratio', label: 'Sortino' },
  { value: 'calmar_ratio', label: 'Calmar' },
  { value: 'profit_factor', label: '盈亏比 (PF)' },
  { value: 'total_return_pct', label: '总回报 %' },
]

const sweepOpen = ref<string[]>(['sweep'])
const sweepRunning = ref(false)
const sweepError = ref('')
const sweepResult = ref<BacktestSweepResult | null>(null)

const sweepForm = ref({
  buyLowRange: { start: 90, step: 5, end: 110 },
  sellHighRange: { start: 190, step: 5, end: 210 },
  minProfitValuesText: '0',
  sortBy: 'sharpe_ratio' as BacktestSweepSortBy,
})

const sweepTopRows = computed(() =>
  sweepResult.value ? sweepResult.value.rows.slice(0, SWEEP_TOP_N) : [],
)

const canSweep = computed(() => (
  csvText.value.trim().length > 0
  && sweepForm.value.buyLowRange.step > 0
  && sweepForm.value.sellHighRange.step > 0
  && sweepForm.value.buyLowRange.end >= sweepForm.value.buyLowRange.start
  && sweepForm.value.sellHighRange.end >= sweepForm.value.sellHighRange.start
))

function metricLabel(key: string): string {
  const found = sortByOptions.find(o => o.value === key)
  return found ? found.label : key
}

function parseNumberList(text: string): number[] {
  return text
    .split(',')
    .map(s => parseFloat(s.trim()))
    .filter(n => !Number.isNaN(n))
}

function formatMetric(metrics: BacktestMetrics, key: string): string {
  const val = metrics[key as keyof BacktestMetrics]
  if (typeof val !== 'number') return '—'
  return val.toFixed(2)
}

function seedSweepFromForm() {
  const bl = form.value.buy_low
  const sh = form.value.sell_high
  sweepForm.value.buyLowRange = { start: Math.max(0.01, bl - 10), step: 5, end: bl + 10 }
  sweepForm.value.sellHighRange = {
    start: Math.max(sh - 10, form.value.buy_low + 1),
    step: 5,
    end: sh + 10,
  }
  ElMessage.success('已根据当前参数生成扫描区间')
}

async function handleRunSweep() {
  if (!canSweep.value) return
  sweepRunning.value = true
  sweepError.value = ''
  try {
    const grid: BacktestSweepGrid = {
      buy_low: {
        range: {
          start: sweepForm.value.buyLowRange.start,
          step: sweepForm.value.buyLowRange.step,
          end: sweepForm.value.buyLowRange.end,
        },
      },
      sell_high: {
        range: {
          start: sweepForm.value.sellHighRange.start,
          step: sweepForm.value.sellHighRange.step,
          end: sweepForm.value.sellHighRange.end,
        },
      },
    }
    const minProfits = parseNumberList(sweepForm.value.minProfitValuesText)
    if (minProfits.length > 0) {
      grid.min_profit_amount = { values: minProfits }
    }
    sweepResult.value = await runBacktestSweep({
      base: { ...form.value, symbol: form.value.symbol.trim().toUpperCase() },
      grid,
      sort_by: sweepForm.value.sortBy,
      max_combinations: 2000,
      csv_text: csvText.value,
    })
    ElMessage.success(`扫描完成：${sweepResult.value.evaluated_count} 组`)
  } catch (err) {
    sweepError.value = resolveErrorMessage(err, '扫描请求失败')
  } finally {
    sweepRunning.value = false
  }
}

function applySweepRow(row: BacktestSweepRow) {
  form.value.buy_low = row.params.buy_low
  form.value.sell_high = row.params.sell_high
  form.value.min_profit_amount = row.params.min_profit_amount
  ElMessage.success('已应用该参数到表单，可运行完整回测查看曲线')
}

const heatmapRows = computed(() => {
  if (!sweepResult.value) return []
  return Array.from(new Set(sweepResult.value.heatmap.cells.map(c => c.buy_low))).sort((a, b) => b - a)
})
const heatmapCols = computed(() => {
  if (!sweepResult.value) return []
  return Array.from(new Set(sweepResult.value.heatmap.cells.map(c => c.sell_high))).sort((a, b) => a - b)
})

function findHeatmapCell(buyLow: number, sellHigh: number): BacktestSweepHeatmapCell | undefined {
  return sweepResult.value?.heatmap.cells.find(c => c.buy_low === buyLow && c.sell_high === sellHigh)
}

function formatHeatmapCell(buyLow: number, sellHigh: number): string {
  const cell = findHeatmapCell(buyLow, sellHigh)
  if (!cell || cell.value === null) return '—'
  return cell.value.toFixed(2)
}

function heatmapCellStyle(buyLow: number, sellHigh: number): CSSProperties {
  const cell = findHeatmapCell(buyLow, sellHigh)
  const values = sweepResult.value?.heatmap.cells
    .map(c => c.value)
    .filter((v): v is number => v !== null) ?? []
  if (!cell || cell.value === null || values.length === 0) return {}
  const min = Math.min(...values)
  const max = Math.max(...values)
  const t = max === min ? 0.5 : (cell.value - min) / (max - min)
  const hue = Math.round(t * 120) // red (0, low) -> green (120, high)
  return { backgroundColor: `hsl(${hue}, 65%, 88%)` }
}

// ---------------------------------------------------------------------------
// Walk-Forward — out-of-sample stability check. Optimizes on each train window
// (reusing the sweep grid above) and evaluates on the following test window.
// ---------------------------------------------------------------------------

const wfForm = ref({
  trainSize: 6,
  testSize: 4,
  step: 0, // 0 = auto (non-overlapping = test_size)
  sortBy: 'sharpe_ratio' as BacktestSweepSortBy,
  optimize: true,
})
const wfResult = ref<WalkForwardResult | null>(null)
const wfRunning = ref(false)
const wfError = ref('')

const canWalkForward = computed(() => (
  csvText.value.trim().length > 0
  && wfForm.value.trainSize >= 2
  && wfForm.value.testSize >= 1
))

function fmt(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? '—' : v.toFixed(digits)
}

function fmtPct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : signedPercent(v)
}

async function handleRunWalkForward() {
  if (!canWalkForward.value) return
  wfRunning.value = true
  wfError.value = ''
  try {
    const grid: BacktestSweepGrid = {}
    if (wfForm.value.optimize) {
      grid.buy_low = {
        range: {
          start: sweepForm.value.buyLowRange.start,
          step: sweepForm.value.buyLowRange.step,
          end: sweepForm.value.buyLowRange.end,
        },
      }
      grid.sell_high = {
        range: {
          start: sweepForm.value.sellHighRange.start,
          step: sweepForm.value.sellHighRange.step,
          end: sweepForm.value.sellHighRange.end,
        },
      }
      const minProfits = parseNumberList(sweepForm.value.minProfitValuesText)
      if (minProfits.length > 0) grid.min_profit_amount = { values: minProfits }
    }
    wfResult.value = await runWalkForward({
      base: { ...form.value, symbol: form.value.symbol.trim().toUpperCase() },
      grid,
      train_size: wfForm.value.trainSize,
      test_size: wfForm.value.testSize,
      step: wfForm.value.step > 0 ? wfForm.value.step : null,
      sort_by: wfForm.value.sortBy,
      max_combinations: 2000,
      csv_text: csvText.value,
    })
    ElMessage.success(`Walk-Forward 完成：${wfResult.value.summary.window_count} 窗口`)
  } catch (err) {
    wfError.value = resolveErrorMessage(err, 'Walk-Forward 请求失败')
  } finally {
    wfRunning.value = false
  }
}

// ---------------------------------------------------------------------------
// What-If stress — Monte-Carlo ensemble over jittered price paths.
// ---------------------------------------------------------------------------

const stressForm = ref({ scenarios: 50, jitterPct: 1.0, seed: 42 })
const stressResult = ref<StressTestResult | null>(null)
const stressRunning = ref(false)
const stressError = ref('')

const canStress = computed(() => csvText.value.trim().length > 0 && stressForm.value.scenarios >= 1)

async function handleRunStress() {
  if (!canStress.value) return
  stressRunning.value = true
  stressError.value = ''
  try {
    stressResult.value = await runStressTest({
      base: { ...form.value, symbol: form.value.symbol.trim().toUpperCase() },
      scenarios: stressForm.value.scenarios,
      jitter_pct: stressForm.value.jitterPct,
      seed: stressForm.value.seed,
      csv_text: csvText.value,
    })
    ElMessage.success(`压力测试完成：${stressResult.value.scenarios_run} 场景`)
  } catch (err) {
    stressError.value = resolveErrorMessage(err, '压力测试请求失败')
  } finally {
    stressRunning.value = false
  }
}

// ---------------------------------------------------------------------------
// Saved-run comparison — persist named runs, then diff metrics side-by-side.
// ---------------------------------------------------------------------------

const savedRuns = ref<BacktestRunOut[]>([])
const saveName = ref('')
const savingRun = ref(false)
const selectedRunIds = ref<number[]>([])
const compareRuns = ref<BacktestRunOut[]>([])

const compareMetricRows: { key: string; label: string; format: (r: BacktestRunOut) => string }[] = [
  { key: 'total_pnl', label: '总收益', format: r => signedCurrency(r.metrics.total_pnl) },
  { key: 'return', label: '回报%', format: r => signedPercent(r.metrics.total_return_pct) },
  { key: 'sharpe', label: 'Sharpe', format: r => fmt(r.metrics.sharpe_ratio) },
  { key: 'mdd', label: '最大回撤%', format: r => fmt(r.metrics.max_drawdown_pct) },
  { key: 'winrate', label: '胜率%', format: r => r.metrics.win_rate.toFixed(1) },
  { key: 'trades', label: '交易笔数', format: r => String(r.metrics.trade_count) },
  { key: 'buy_low', label: 'buy_low', format: r => r.params.buy_low.toFixed(2) },
  { key: 'sell_high', label: 'sell_high', format: r => r.params.sell_high.toFixed(2) },
]

async function loadSavedRuns() {
  try {
    savedRuns.value = (await listBacktestRuns()).items
  } catch {
    // non-fatal
  }
}

function onSelectionChange(rows: BacktestRunOut[]) {
  selectedRunIds.value = rows.map(r => r.id)
}

async function handleSaveRun() {
  if (!result.value || !saveName.value.trim()) return
  savingRun.value = true
  try {
    await saveBacktestRun({
      name: saveName.value.trim(),
      params: result.value.params,
      metrics: result.value.metrics,
    })
    saveName.value = ''
    await loadSavedRuns()
    ElMessage.success('已保存为对比快照')
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '保存失败'))
  } finally {
    savingRun.value = false
  }
}

async function handleDeleteRun(id: number) {
  try {
    await deleteBacktestRun(id)
    await loadSavedRuns()
  } catch {
    ElMessage.error('删除失败')
  }
}

async function handleCompare() {
  if (selectedRunIds.value.length < 2) return
  try {
    compareRuns.value = (await compareBacktestRuns(selectedRunIds.value)).runs
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '对比失败'))
  }
}

function signedCurrency(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}

function signedPercent(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+${amount}%`
  if (normalized < 0) return `-${amount}%`
  return `${amount}%`
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function metricClass(value: number | null | undefined): string {
  const normalized = value ?? 0
  if (normalized > 0) return 'metric-positive'
  if (normalized < 0) return 'metric-negative'
  return ''
}

function metricTagType(value: number): string {
  if (value > 0) return 'success'
  if (value < 0) return 'danger'
  return 'info'
}

function stateLabel(value: string): string {
  if (value === 'long') return '持多'
  if (value === 'short') return '持空'
  return '空仓'
}

function actionLabel(action: string): string {
  const labels: Record<string, string> = {
    BUY: '买入',
    SELL: '卖出',
    SELL_SHORT: '开空',
    BUY_TO_COVER: '平空',
    STOP_LOSS_SELL: '止损卖出',
    STOP_LOSS_COVER: '止损平空',
  }
  return labels[action] ?? action
}

function actionTagType(action: string): string {
  if (action.startsWith('STOP_LOSS')) return 'warning'
  if (action === 'BUY' || action === 'BUY_TO_COVER') return 'success'
  if (action === 'SELL' || action === 'SELL_SHORT') return 'danger'
  return 'info'
}

onMounted(() => {
  loadCurrentStrategy().catch(() => void 0)
  loadSavedRuns()
})
</script>

<style scoped>
.backtest-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-heading h3 {
  margin: 0;
}

.page-heading p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.heading-tags,
.panel-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.backtest-tool,
.result-grid {
  display: grid;
  grid-template-columns: minmax(360px, .95fr) minmax(420px, 1.15fr);
  gap: 12px;
}

.tool-panel,
.result-panel,
.metric-item {
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  background: #fff;
}

.tool-panel,
.result-panel {
  padding: 14px;
}

.panel-heading,
.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.panel-heading h4,
.section-title h4,
.subsection-title {
  margin: 0;
  color: #172033;
  font-size: 15px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 10px;
}

.params-panel :deep(.el-input-number) {
  width: 100%;
}

.file-input {
  display: none;
}

.run-row {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 12px;
}

.broker-fetch {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.broker-fetch-label {
  color: #6b7280;
  font-size: 12px;
}

.run-row :deep(.el-alert) {
  flex: 1 1 auto;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
}

.metric-item {
  min-height: 82px;
  padding: 10px 12px;
}

.metric-item span {
  display: block;
  color: #6b7280;
  font-size: 12px;
}

.metric-item strong {
  display: block;
  margin-top: 5px;
  color: #172033;
  font-size: 20px;
  line-height: 1.1;
}

.metric-item small {
  display: block;
  margin-top: 5px;
  color: #7a8595;
  font-size: 12px;
}

.responsive-table {
  width: 100%;
}

.skip-list {
  display: grid;
  gap: 8px;
  margin-bottom: 16px;
}

.skip-row {
  border-radius: 6px;
  padding: 9px;
  background: #f8fafc;
}

.skip-row strong,
.skip-row span {
  display: block;
}

.skip-row strong {
  color: #172033;
  font-size: 13px;
}

.skip-row span {
  margin-top: 3px;
  color: #7a8595;
  font-size: 12px;
}

.skip-row p {
  margin: 6px 0 0;
  color: #4b5563;
  font-size: 12px;
  line-height: 1.45;
}

.subsection-title {
  margin: 16px 0 10px;
}

.empty-note {
  margin: 24px 0;
  color: #999;
  text-align: center;
}

.metric-positive {
  color: #14884f !important;
}

.metric-negative {
  color: #c43838 !important;
}

@media (max-width: 1280px) {
  .metrics-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 960px) {
  .backtest-tool,
  .result-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .page-heading,
  .panel-heading {
    flex-direction: column;
    align-items: flex-start;
  }

  .heading-tags,
  .panel-actions {
    justify-content: flex-start;
  }

  .form-grid,
  .metrics-grid {
    grid-template-columns: 1fr;
  }
}

.sweep-section {
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  background: #fff;
  padding: 6px 14px;
}

.sweep-section :deep(.el-collapse) {
  border: none;
}

.sweep-section :deep(.el-collapse-item__header) {
  font-size: 15px;
  font-weight: 600;
  color: #172033;
}

.sweep-title {
  margin-right: 10px;
}

.sweep-title-tag {
  margin-left: 4px;
}

.sweep-intro {
  margin: 4px 0 12px;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.5;
}

.sweep-grid-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.sweep-axis {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sweep-axis label {
  color: #6b7280;
  font-size: 12px;
}

.sweep-axis .triple {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
}

.sweep-axis :deep(.el-input-number),
.sweep-axis :deep(.el-select),
.sweep-axis :deep(.el-input) {
  width: 100%;
}

.sweep-results {
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr);
  gap: 12px;
  margin-top: 14px;
}

.wf-results {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 14px;
}

.wf-summary {
  grid-template-columns: repeat(6, minmax(0, 1fr));
}

.sweep-table :deep(.el-table__row) {
  cursor: pointer;
}

.sweep-hint {
  margin: 8px 0 0;
  color: #7a8595;
  font-size: 12px;
}

.sweep-note {
  margin: 8px 0 0;
  color: #b7791f;
  font-size: 12px;
}

.heatmap-wrap {
  overflow-x: auto;
}

.heatmap-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.heatmap-table th,
.heatmap-table td {
  border: 1px solid #e1e7f0;
  padding: 6px 8px;
  text-align: center;
  white-space: nowrap;
}

.heatmap-table thead th,
.heatmap-table tbody th {
  background: #f8fafc;
  color: #4b5563;
  font-weight: 600;
}

.heatmap-table td {
  color: #172033;
}

.compare-table th {
  text-align: left;
}

@media (max-width: 960px) {
  .sweep-results {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .sweep-grid-form {
    grid-template-columns: 1fr;
  }

  .sweep-axis .triple {
    grid-template-columns: 1fr;
  }
}
</style>
