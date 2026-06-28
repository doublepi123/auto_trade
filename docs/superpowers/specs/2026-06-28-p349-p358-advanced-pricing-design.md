# P349–P358：高级定价、自适应配置与归因分析（10 轮自主迭代）

## 背景
承接 P339–P348 系统性风险与期权面智能，新增 10 个 `backend/app/platform/*` 纯 Python 模块与 10 个 `/api/platform/*` 只读端点。参考 Lévy 过程（VG/CGMY）、LASSO/Ridge 坐标下降、多资产 Kelly、Bollinger/Keltner Squeeze、Relative Rotation Graph、日历效应、贝叶斯模型平均、隐含相关性、互信息配对筛选、Grinold-Kahn 活跃归因。

## 非目标
不改实盘/风控/broker/runner；不新增 DB 表/前端/Cypress；不引入新依赖；不把诊断输出自动用于下单。

## 功能拆分
| 轮 | 模块 | 端点 | 核心 |
|---|---|---|---|
| P349 | levy_processes.py | /levy-processes | VG/CGMY 特征函数定价 |
| P350 | penalized_regression.py | /penalized-regression | Ridge/LASSO 坐标下降 |
| P351 | multi_asset_kelly.py | /multi-kelly | 多资产 Kelly 最优配置 |
| P352 | squeeze_detection.py | /squeeze-detection | BB+Keltner 波动率压缩 |
| P353 | relative_rotation.py | /relative-rotation | RRG 四象限评分 |
| P354 | seasonality.py | /seasonality | 日历效应统计检验 |
| P355 | bayesian_model_averaging.py | /bayesian-model-averaging | BIC 后验 BMA 融合 |
| P356 | implied_correlation.py | /implied-correlation | 个股 IV 推导隐含相关 |
| P357 | pair_screening.py | /pair-screening | 互信息+距离相关配对筛选 |
| P358 | active_attribution.py | /active-attribution | Grinold-Kahn 活跃归因 |

## 约束
纯函数 + frozen dataclass + to_dict()；复用 factor_utils；面板 ≤50 资产、序列 ≤5000；非法输入 ValueError，API 422；矩阵/回归小规模确定性算法。

## 测试
每轮 TDD；最终目标测试 + basedpyright + 全量 pytest + Oracle review。

## 自审
无 TBD；范围只读；10 模块边界独立；与 P149–P348 区分明确。