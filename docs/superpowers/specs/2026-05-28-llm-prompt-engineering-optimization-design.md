# LLM Prompt Engineering Optimization Design

> **目标：** 通过架构重构和模块化设计，提升 LLM 交易顾问的决策质量、响应速度和可维护性。

**Architecture:** 简化 DDD 分层架构（领域层、应用层、接口层）

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / SQLite / pytest / Vue 3 + TypeScript + Element Plus / Cypress

**Baseline (2026-05-28):** `pytest 493 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

**Estimated Effort:** 2 周（第一阶段 1 周，第二阶段 1 周）

---

## 迭代目标 (Sprint Goal)

> 通过模块化 prompt 架构、技术指标扩展和 A/B 测试支持，显著提升 LLM 交易顾问的决策质量和系统性能。

**完成定义 (Definition of Done):**
1. 模块化 prompt 架构完成，支持动态组合
2. 技术指标扩展完成（RSI、MACD、成交量分析）
3. A/B 测试框架完成，支持 prompt 版本管理
4. 市场情绪数据集成完成
5. 多时间框架分析支持
6. 性能追踪和优化完成
7. `pytest` 新增 ≥20 项，`npm run type-check` + `npm run build` 通过

---

## 架构设计

### 分层架构

```
┌─────────────────────────────────────────┐
│           接口层 (Interface Layer)       │
│  - RESTful API (FastAPI)                │
│  - WebSocket                            │
│  - Web UI (Vue 3)                       │
└─────────────────────────────────────────┘
                    │
┌─────────────────────────────────────────┐
│           应用层 (Application Layer)     │
│  - 用例编排 (Use Case Orchestration)     │
│  - 流程控制 (Process Control)           │
│  - 外部集成 (External Integration)      │
└─────────────────────────────────────────┘
                    │
┌─────────────────────────────────────────┐
│           领域层 (Domain Layer)          │
│  - 核心业务逻辑 (Core Business Logic)   │
│  - 领域模型 (Domain Models)             │
│  - 领域服务 (Domain Services)           │
└─────────────────────────────────────────┘
```

### 核心领域模块

1. **PromptBuilder** - 模块化 prompt 构建
   - SystemModule - 基础指令和角色定义
   - ContextModule - 市场数据、技术指标、账户状态
   - StrategyModule - 交易策略和风险规则
   - OutputModule - 输出格式和约束

2. **TechnicalAnalyzer** - 技术指标计算和分析
   - RSI (Relative Strength Index)
   - MACD (Moving Average Convergence Divergence)
   - 成交量分析
   - 支撑阻力位识别

3. **RiskAssessor** - 风险评估和止损策略
   - 动态止损点计算
   - 风险收益比评估
   - 仓位管理建议

4. **SignalGenerator** - 交易信号生成
   - 买入信号
   - 卖出信号
   - 止损信号
   - 持仓调整信号

### 应用层模块

1. **用例编排模块**
   - LLM 分析流程
   - 数据聚合流程
   - 信号处理流程

2. **流程控制模块**
   - A/B 测试管理
   - 性能追踪
   - 结果评估

3. **外部集成模块**
   - LLM API 调用
   - 数据源集成
   - 通知服务

### 接口层模块

1. **API 接口模块**
   - RESTful API
   - WebSocket
   - gRPC (可选)

2. **外部接口模块**
   - LLM API
   - 数据源 API
   - 通知 API

3. **用户界面模块**
   - Web UI
   - 移动端
   - CLI

---

## 第一阶段实现（1 周）

### 任务 1：模块化 Prompt 架构

**目标：** 将现有单体重构为模块化组件，支持动态组合和 A/B 测试。

**文件变更：**
- 新建：`backend/app/domain/prompt/` 目录
  - `system_module.py` - 系统指令模块
  - `context_module.py` - 上下文数据模块
  - `strategy_module.py` - 策略规则模块
  - `output_module.py` - 输出格式模块
  - `prompt_builder.py` - Prompt 构建器
- 修改：`backend/app/services/data_aggregator.py` - 集成新的 PromptBuilder
- 修改：`backend/app/services/llm_advisor_service.py` - 使用新的 PromptBuilder

**实现细节：**

```python
# backend/app/domain/prompt/prompt_builder.py
class PromptBuilder:
    def __init__(self):
        self.modules = []
    
    def add_module(self, module: PromptModule) -> 'PromptBuilder':
        self.modules.append(module)
        return self
    
    def build(self, context: dict) -> str:
        parts = []
        for module in self.modules:
            parts.append(module.render(context))
        return "\n\n".join(parts)
```

### 任务 2：技术指标扩展

**目标：** 添加 RSI、MACD、成交量分析等技术指标。

**文件变更：**
- 新建：`backend/app/domain/analysis/` 目录
  - `technical_indicators.py` - 技术指标计算
  - `rsi.py` - RSI 计算
  - `macd.py` - MACD 计算
  - `volume_analysis.py` - 成交量分析
- 修改：`backend/app/services/data_aggregator.py` - 集成新的技术指标

**实现细节：**

```python
# backend/app/domain/analysis/technical_indicators.py
class TechnicalIndicators:
    @staticmethod
    def calculate_rsi(closes: list[float], period: int = 14) -> float:
        # RSI 计算逻辑
        pass
    
    @staticmethod
    def calculate_macd(closes: list[float]) -> dict:
        # MACD 计算逻辑
        pass
    
    @staticmethod
    def analyze_volume(volumes: list[float]) -> dict:
        # 成交量分析逻辑
        pass
```

### 任务 3：A/B 测试支持

**目标：** 支持 prompt 版本管理和 A/B 测试。

**文件变更：**
- 新建：`backend/app/domain/experiment/` 目录
  - `ab_test_manager.py` - A/B 测试管理
  - `prompt_version.py` - Prompt 版本管理
  - `performance_tracker.py` - 性能追踪
- 新建：`backend/app/models.py` - 添加 PromptVersion 和 ExperimentResult 模型
- 新建：`backend/app/api/experiments.py` - 实验管理 API

**实现细节：**

```python
# backend/app/domain/experiment/ab_test_manager.py
class ABTestManager:
    def create_experiment(self, name: str, variants: list[PromptVersion]) -> Experiment:
        # 创建实验
        pass
    
    def get_variant(self, experiment_id: str) -> PromptVersion:
        # 获取实验变体
        pass
    
    def record_result(self, experiment_id: str, variant_id: str, result: dict):
        # 记录实验结果
        pass
```

---

## 第二阶段实现（1 周）

### 任务 4：市场情绪数据集成

**目标：** 整合新闻情绪、社交媒体分析、市场情绪指标。

**文件变更：**
- 新建：`backend/app/domain/sentiment/` 目录
  - `news_sentiment.py` - 新闻情绪分析
  - `social_sentiment.py` - 社交媒体情绪分析
  - `market_sentiment.py` - 市场情绪指标
- 修改：`backend/app/services/data_aggregator.py` - 集成情绪数据

### 任务 5：多时间框架分析

**目标：** 支持日线、小时线、分钟线等多时间框架分析。

**文件变更：**
- 修改：`backend/app/domain/analysis/technical_indicators.py` - 支持多时间框架
- 修改：`backend/app/services/data_aggregator.py` - 聚合多时间框架数据

### 任务 6：性能追踪和优化

**目标：** 实现性能追踪、结果评估和优化建议。

**文件变更：**
- 新建：`backend/app/domain/performance/` 目录
  - `performance_tracker.py` - 性能追踪
  - `result_evaluator.py` - 结果评估
  - `optimization_advisor.py` - 优化建议
- 新建：`backend/app/api/performance.py` - 性能查询 API

---

## 数据模型

### PromptVersion

```python
class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)
```

### ExperimentResult

```python
class ExperimentResult(Base):
    __tablename__ = "experiment_results"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(50), nullable=False)
    variant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    accuracy: Mapped[float] = mapped_column(Float, default=0.0)
    profit_loss: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)
```

---

## API 设计

### 实验管理 API

```python
# POST /api/experiments - 创建实验
# GET /api/experiments - 获取实验列表
# GET /api/experiments/{id} - 获取实验详情
# POST /api/experiments/{id}/start - 启动实验
# POST /api/experiments/{id}/stop - 停止实验
# GET /api/experiments/{id}/results - 获取实验结果
```

### 性能查询 API

```python
# GET /api/performance/accuracy - 获取准确率
# GET /api/performance/profit-loss - 获取盈亏
# GET /api/performance/win-rate - 获取胜率
# GET /api/performance/recommendations - 获取优化建议
```

---

## 测试策略

### 单元测试

- PromptBuilder 测试
- TechnicalIndicators 测试
- RiskAssessor 测试
- SignalGenerator 测试

### 集成测试

- LLM 分析流程测试
- 数据聚合流程测试
- A/B 测试流程测试

### 端到端测试

- 完整交易流程测试
- 性能追踪测试
- 实验管理测试

---

## 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 模块化重构引入 bug | 交易决策错误 | 充分测试，渐进式迁移 |
| 技术指标计算错误 | 信号不准确 | 单元测试覆盖，对比验证 |
| A/B 测试框架复杂 | 实现周期延长 | 简化实现，先支持基本功能 |
| 市场情绪数据质量 | 分析不准确 | 多数据源验证，异常检测 |

---

## 验证标准

### 第一阶段验证

- [ ] 模块化 prompt 架构完成
- [ ] 技术指标扩展完成（RSI、MACD、成交量）
- [ ] A/B 测试框架完成
- [ ] `pytest` 新增 ≥10 项
- [ ] `npm run type-check` + `npm run build` 通过

### 第二阶段验证

- [ ] 市场情绪数据集成完成
- [ ] 多时间框架分析支持
- [ ] 性能追踪和优化完成
- [ ] `pytest` 新增 ≥10 项
- [ ] `npm run type-check` + `npm run build` 通过

---

## 后续优化

1. **机器学习模型集成** - 使用历史数据训练交易模型
2. **实时数据流处理** - 支持实时市场数据处理
3. **分布式计算** - 支持大规模数据分析
4. **可视化仪表盘** - 实时展示分析结果和性能指标
