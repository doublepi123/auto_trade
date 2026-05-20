# Preview LLM Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users enter a stock symbol, run an LLM analysis preview without saving the strategy, then save/apply the preview only after confirmation.

**Architecture:** Add a backend preview endpoint that reuses `LLMAdvisorService` market-data and LLM prompt logic but does not record analysis or apply interval changes. Add a Strategy page preview panel and confirmation flow that calls the preview endpoint first, then saves the strategy with the previewed symbol and suggested interval if the user confirms.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy service patterns, Vue 3 `<script setup>`, Element Plus, Cypress, pytest.

---

## File Structure

- Modify `backend/app/schemas.py`: add request/response schemas for preview analysis.
- Modify `backend/app/services/llm_advisor_service.py`: extract preview-safe analysis path that calls DeepSeek without throttling side effects or DB writes.
- Modify `backend/app/api/llm_advisor.py`: add `POST /api/strategy/llm-interval/preview`.
- Modify `backend/tests/test_llm_advisor.py`: unit-test preview analysis side-effect boundaries and schema-level behavior.
- Modify `frontend/src/types/index.ts`: add `LLMPreviewAnalyzeRequest` and typed `LLMAnalyzeResponse`.
- Modify `frontend/src/api/llm_advisor.ts`: add `previewLLMInterval()` and type existing analysis response.
- Modify `frontend/src/views/Strategy.vue`: add preview button/panel and save/apply confirmation flow.
- Modify `frontend/cypress/support/e2e.ts`: stub preview endpoint.
- Modify `frontend/cypress/e2e/strategy_llm.cy.ts`: cover preview-before-save behavior.

## Tasks

### Task 1: Backend Preview Contract

**Files:**
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_llm_advisor.py`

- [ ] **Step 1: Add failing schema tests**

Append these tests to `backend/tests/test_llm_advisor.py`:

```python
from app.schemas import LLMPreviewAnalyzeRequest


def test_preview_request_normalizes_symbol() -> None:
    payload = LLMPreviewAnalyzeRequest(symbol=" aapl.us ", market="US")

    assert payload.symbol == "AAPL.US"


def test_preview_request_requires_supported_market() -> None:
    with pytest.raises(ValueError):
        LLMPreviewAnalyzeRequest(symbol="AAPL.US", market="CN")
```

- [ ] **Step 2: Run schema tests and verify failure**

Run: `cd backend && python3 -m pytest tests/test_llm_advisor.py::test_preview_request_normalizes_symbol tests/test_llm_advisor.py::test_preview_request_requires_supported_market -v`

Expected: FAIL because `LLMPreviewAnalyzeRequest` is not defined.

- [ ] **Step 3: Add preview schemas**

In `backend/app/schemas.py`, add this after `LLMAnalyzeRequest`:

```python
class LLMPreviewAnalyzeRequest(BaseModel):
    symbol: str = Field(max_length=50)
    market: str = Field(default="US")
    current_price: Optional[float] = Field(default=None, gt=0)
    current_buy_low: Optional[float] = Field(default=None, ge=0)
    current_sell_high: Optional[float] = Field(default=None, ge=0)
    short_selling: bool = Field(default=False)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _normalize_symbol(v)

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        if v not in ("US", "HK"):
            raise ValueError("market must be US or HK")
        return v
```

- [ ] **Step 4: Run schema tests and verify pass**

Run: `cd backend && python3 -m pytest tests/test_llm_advisor.py::test_preview_request_normalizes_symbol tests/test_llm_advisor.py::test_preview_request_requires_supported_market -v`

Expected: PASS.

### Task 2: Backend Preview Analysis Service

**Files:**
- Modify: `backend/app/services/llm_advisor_service.py`
- Test: `backend/tests/test_llm_advisor.py`

- [ ] **Step 1: Add failing service test**

Append this test inside `TestLLMAdvisorService` in `backend/tests/test_llm_advisor.py`:

```python
    def test_preview_does_not_record_or_throttle(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        import app.services.llm_advisor_service as service_module

        monkeypatch.setattr(service_module, "_LAST_ANALYSIS_TIMESTAMP", 123.0)
        monkeypatch.setattr(
            advisor._data_aggregator,
            "fetch_market_data",
            lambda symbol, market: {
                "daily_candles": [],
                "minute_candles": [],
                "current_price": 205.0,
                "atr": 5.0,
                "bb_upper": 215.0,
                "bb_middle": 205.0,
                "bb_lower": 195.0,
            },
        )
        monkeypatch.setattr(
            advisor,
            "_call_deepseek",
            lambda prompt: '{"suggested_buy_low": 200.0, "suggested_sell_high": 210.0, "confidence_score": 0.82, "analysis": "preview"}',
        )
        monkeypatch.setattr(
            advisor,
            "_record_analysis",
            lambda *args, **kwargs: pytest.fail("preview must not record analysis"),
        )

        result = advisor.preview(
            symbol="AAPL.US",
            market="US",
            current_price=0.0,
            current_buy_low=0.0,
            current_sell_high=0.0,
            short_selling=False,
        )

        assert result["success"] is True
        assert result["applied"] is False
        assert result["suggested_buy_low"] == 200.0
        assert result["suggested_sell_high"] == 210.0
        assert service_module._LAST_ANALYSIS_TIMESTAMP == 123.0
```

- [ ] **Step 2: Run service test and verify failure**

Run: `cd backend && python3 -m pytest tests/test_llm_advisor.py::TestLLMAdvisorService::test_preview_does_not_record_or_throttle -v`

Expected: FAIL because `LLMAdvisorService.preview` is not defined.

- [ ] **Step 3: Implement preview service**

In `backend/app/services/llm_advisor_service.py`, add this public method after `analyze()`:

```python
    def preview(
        self,
        symbol: str,
        market: str,
        current_price: float,
        current_buy_low: float,
        current_sell_high: float,
        short_selling: bool,
    ) -> dict[str, Any]:
        """Run LLM analysis without throttling, recording, or applying suggestions."""
        try:
            market_data = self._data_aggregator.fetch_market_data(symbol, market)
        except Exception:
            logger.exception("failed to fetch market data for LLM preview")
            market_data = {
                "daily_candles": [],
                "minute_candles": [],
                "current_price": current_price,
                "atr": 0.0,
                "bb_upper": 0.0,
                "bb_middle": 0.0,
                "bb_lower": 0.0,
            }

        prompt_price = market_data.get("current_price") or current_price
        prompt = self._data_aggregator.build_prompt(
            symbol=symbol,
            market=market,
            current_price=prompt_price,
            current_buy_low=current_buy_low,
            current_sell_high=current_sell_high,
            short_selling=short_selling,
            daily_candles=market_data.get("daily_candles", []),
            minute_candles=market_data.get("minute_candles", []),
            atr=market_data.get("atr", 0.0),
            bb_upper=market_data.get("bb_upper", 0.0),
            bb_middle=market_data.get("bb_middle", 0.0),
            bb_lower=market_data.get("bb_lower", 0.0),
            current_position="FLAT",
            recent_trades=[],
        )

        try:
            raw_response = self._call_deepseek(prompt)
            result = self._parse_response(raw_response)
        except Exception as exc:
            logger.exception("LLM preview failed")
            return {"success": False, "applied": False, "error": f"LLM preview failed: {exc}"}

        return {
            "success": True,
            "applied": False,
            "reason": "Preview completed. Confirm to save and apply.",
            "suggested_buy_low": result.get("suggested_buy_low"),
            "suggested_sell_high": result.get("suggested_sell_high"),
            "confidence_score": result.get("confidence_score"),
            "analysis": result.get("analysis"),
            "next_analysis_at": None,
            "applied_at": None,
        }
```

- [ ] **Step 4: Run service test and verify pass**

Run: `cd backend && python3 -m pytest tests/test_llm_advisor.py::TestLLMAdvisorService::test_preview_does_not_record_or_throttle -v`

Expected: PASS.

### Task 3: Backend Preview API

**Files:**
- Modify: `backend/app/api/llm_advisor.py`
- Test: `backend/tests/test_llm_advisor.py`

- [ ] **Step 1: Add API unit test**

Append this test to `backend/tests/test_llm_advisor.py`:

```python
def test_preview_endpoint_uses_payload_without_saving(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.llm_advisor_service import LLMAdvisorService

    captured = {}

    def fake_preview(self, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "applied": False,
            "reason": "Preview completed. Confirm to save and apply.",
            "suggested_buy_low": 200.0,
            "suggested_sell_high": 210.0,
            "confidence_score": 0.82,
            "analysis": "preview",
            "next_analysis_at": None,
            "applied_at": None,
        }

    monkeypatch.setattr(LLMAdvisorService, "preview", fake_preview)
    client = TestClient(app)

    response = client.post(
        "/api/strategy/llm-interval/preview",
        json={"symbol": " aapl.us ", "market": "US", "current_buy_low": 0, "current_sell_high": 0},
    )

    assert response.status_code == 200
    assert response.json()["analysis"] == "preview"
    assert captured["symbol"] == "AAPL.US"
    assert captured["market"] == "US"
```

- [ ] **Step 2: Run API test and verify failure**

Run: `cd backend && python3 -m pytest tests/test_llm_advisor.py::test_preview_endpoint_uses_payload_without_saving -v`

Expected: FAIL with 404 or import error for missing request schema.

- [ ] **Step 3: Add endpoint**

In `backend/app/api/llm_advisor.py`, update imports:

```python
from app.schemas import LLMAnalyzeRequest, LLMAnalyzeResponse, LLMIntervalStatus, LLMPreviewAnalyzeRequest, MessageResponse
```

Then add this route before `analyze_llm_interval()`:

```python
@router.post("/strategy/llm-interval/preview", response_model=LLMAnalyzeResponse)
def preview_llm_interval(payload: LLMPreviewAnalyzeRequest) -> LLMAnalyzeResponse:
    advisor = LLMAdvisorService()
    result = advisor.preview(
        symbol=payload.symbol,
        market=payload.market,
        current_price=payload.current_price or 0.0,
        current_buy_low=payload.current_buy_low or 0.0,
        current_sell_high=payload.current_sell_high or 0.0,
        short_selling=payload.short_selling,
    )
    if not result["success"]:
        return LLMAnalyzeResponse(success=False, applied=False, reason=result.get("error", "Unknown error"))
    return LLMAnalyzeResponse(
        success=True,
        applied=False,
        reason=result["reason"],
        suggested_buy_low=result.get("suggested_buy_low"),
        suggested_sell_high=result.get("suggested_sell_high"),
        confidence_score=result.get("confidence_score"),
        analysis=result.get("analysis"),
        next_analysis_at=None,
        applied_at=None,
    )
```

- [ ] **Step 4: Run API test and verify pass**

Run: `cd backend && python3 -m pytest tests/test_llm_advisor.py::test_preview_endpoint_uses_payload_without_saving -v`

Expected: PASS.

### Task 4: Frontend API and Types

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/llm_advisor.ts`

- [ ] **Step 1: Add typed contracts**

In `frontend/src/types/index.ts`, add after `LLMIntervalStatus`:

```typescript
export interface LLMAnalyzeResponse {
  success: boolean
  applied: boolean
  reason: string
  suggested_buy_low?: number | null
  suggested_sell_high?: number | null
  confidence_score?: number | null
  analysis?: string | null
  next_analysis_at?: string | null
  applied_at?: string | null
}

export interface LLMPreviewAnalyzeRequest {
  symbol: string
  market: 'US' | 'HK'
  current_price?: number | null
  current_buy_low?: number | null
  current_sell_high?: number | null
  short_selling: boolean
}
```

- [ ] **Step 2: Add preview API wrapper**

Change `frontend/src/api/llm_advisor.ts` to:

```typescript
import { api } from './client'
import type { LLMAnalyzeResponse, LLMIntervalStatus, LLMPreviewAnalyzeRequest } from '../types'

export async function getLLMIntervalStatus(): Promise<LLMIntervalStatus> {
  const resp = await api.get('/api/strategy/llm-interval/status')
  return resp.data
}

export async function previewLLMInterval(payload: LLMPreviewAnalyzeRequest): Promise<LLMAnalyzeResponse> {
  const resp = await api.post('/api/strategy/llm-interval/preview', payload, { timeout: 90000 })
  return resp.data
}

export async function analyzeLLMInterval(force = false): Promise<LLMAnalyzeResponse> {
  const resp = await api.post('/api/strategy/llm-interval/analyze', { force }, { timeout: 90000 })
  return resp.data
}

export async function enableLLMInterval(): Promise<void> {
  await api.put('/api/strategy/llm-interval/enable')
}

export async function disableLLMInterval(): Promise<void> {
  await api.put('/api/strategy/llm-interval/disable')
}
```

- [ ] **Step 3: Run frontend type check**

Run: `cd frontend && npm run build`

Expected: PASS or fail only because `previewLLMInterval` is unused until Task 5.

### Task 5: Strategy Page Preview Flow

**Files:**
- Modify: `frontend/src/views/Strategy.vue`

- [ ] **Step 1: Import preview types/API**

Change the import from `../api` to include `previewLLMInterval`:

```typescript
import { getStrategy, updateStrategy, getLLMIntervalStatus, analyzeLLMInterval, previewLLMInterval, enableLLMInterval, disableLLMInterval } from '../api'
```

Change type imports to:

```typescript
import type { LLMAnalyzeResponse, LLMIntervalStatus } from '../types'
```

- [ ] **Step 2: Add preview state**

After `const analyzing = ref(false)`, add:

```typescript
const previewing = ref(false)
const savingPreview = ref(false)
const previewResult = ref<LLMAnalyzeResponse | null>(null)
const previewSymbol = ref('')
```

- [ ] **Step 3: Add preview handlers**

Before `triggerAnalyze`, add:

```typescript
const previewAnalyze = async () => {
  previewing.value = true
  previewResult.value = null
  previewSymbol.value = form.symbol.trim().toUpperCase()
  try {
    const result = await previewLLMInterval({
      symbol: previewSymbol.value,
      market: form.market,
      current_buy_low: form.buy_low,
      current_sell_high: form.sell_high,
      short_selling: form.short_selling,
    })
    previewResult.value = result
    if (result.success) {
      ElMessage.success('预分析完成，请确认后保存')
    } else {
      ElMessage.warning(result.reason)
    }
  } catch {
    ElMessage.error('预分析失败')
  } finally {
    previewing.value = false
  }
}

const savePreview = async () => {
  if (!previewResult.value?.success || !previewResult.value.suggested_buy_low || !previewResult.value.suggested_sell_high) return
  savingPreview.value = true
  try {
    form.symbol = previewSymbol.value
    form.buy_low = previewResult.value.suggested_buy_low
    form.sell_high = previewResult.value.suggested_sell_high
    await save()
    ElMessage.success('分析结果已保存到策略')
    previewResult.value = null
    await loadLLMStatus()
  } catch {
    ElMessage.error('保存分析结果失败')
  } finally {
    savingPreview.value = false
  }
}
```

- [ ] **Step 4: Add preview UI**

In the LLM card button area, replace the existing manual analyze button block with:

```vue
<el-space wrap>
  <el-button size="small" type="primary" plain :loading="previewing" :disabled="!form.symbol" @click="previewAnalyze">
    先分析
  </el-button>
  <el-button size="small" :loading="analyzing" @click="triggerAnalyze">
    立即重新分析当前策略
  </el-button>
</el-space>
```

After that button area, add:

```vue
<el-alert
  v-if="previewResult"
  :type="previewResult.success ? 'success' : 'warning'"
  :title="previewResult.success ? '预分析完成' : '预分析未完成'"
  show-icon
  :closable="false"
  style="margin-top: 12px"
>
  <template #default>
    <p v-if="previewResult.analysis">分析: {{ previewResult.analysis }}</p>
    <p v-if="previewResult.suggested_buy_low && previewResult.suggested_sell_high">
      建议区间: {{ previewResult.suggested_buy_low.toFixed(2) }} ~ {{ previewResult.suggested_sell_high.toFixed(2) }}
    </p>
    <p v-if="previewResult.confidence_score">置信度: {{ previewResult.confidence_score }}</p>
    <p>{{ previewResult.reason }}</p>
    <el-button
      v-if="previewResult.success && previewResult.suggested_buy_low && previewResult.suggested_sell_high"
      size="small"
      type="success"
      :loading="savingPreview"
      @click="savePreview"
    >
      保存并应用到策略
    </el-button>
  </template>
</el-alert>
```

- [ ] **Step 5: Run frontend build**

Run: `cd frontend && npm run build`

Expected: PASS.

### Task 6: Cypress Coverage

**Files:**
- Modify: `frontend/cypress/support/e2e.ts`
- Modify: `frontend/cypress/e2e/strategy_llm.cy.ts`

- [ ] **Step 1: Stub preview endpoint**

In `frontend/cypress/support/e2e.ts`, add after the LLM status intercept:

```typescript
  cy.intercept('POST', '/api/strategy/llm-interval/preview', {
    body: {
      success: true,
      applied: false,
      reason: 'Preview completed. Confirm to save and apply.',
      suggested_buy_low: 198,
      suggested_sell_high: 212,
      confidence_score: 0.82,
      analysis: '预分析测试',
      next_analysis_at: null,
      applied_at: null,
    },
  }).as('previewLLMInterval')
```

- [ ] **Step 2: Update existing button assertion**

In `frontend/cypress/e2e/strategy_llm.cy.ts`, change `cy.contains('立即重新分析').should('be.visible')` to:

```typescript
    cy.contains('先分析').should('be.visible')
    cy.contains('立即重新分析当前策略').should('be.visible')
```

- [ ] **Step 3: Add preview-save test**

Append this test:

```typescript
  it('previews symbol analysis before saving strategy', () => {
    cy.visitApp('/strategy')
    cy.get('input').first().clear().type('AAPL.US')
    cy.contains('先分析').click()
    cy.wait('@previewLLMInterval')
    cy.contains('预分析测试').should('be.visible')
    cy.contains('198.00 ~ 212.00').should('be.visible')
    cy.contains('保存并应用到策略').click()
    cy.wait('@saveStrategy')
  })
```

- [ ] **Step 4: Run Cypress target spec**

Run: `cd frontend && npm run cypress:run -- --spec cypress/e2e/strategy_llm.cy.ts`

Expected: PASS.

### Task 7: Verification

**Files:**
- All changed files.

- [ ] **Step 1: Run backend LLM tests**

Run: `cd backend && python3 -m pytest tests/test_llm_advisor.py -v`

Expected: PASS.

- [ ] **Step 2: Run frontend build**

Run: `cd frontend && npm run build`

Expected: PASS.

- [ ] **Step 3: Run targeted Cypress spec**

Run: `cd frontend && npm run cypress:run -- --spec cypress/e2e/strategy_llm.cy.ts`

Expected: PASS.

- [ ] **Step 4: Inspect git diff**

Run: `git diff -- backend/app/schemas.py backend/app/services/llm_advisor_service.py backend/app/api/llm_advisor.py backend/tests/test_llm_advisor.py frontend/src/types/index.ts frontend/src/api/llm_advisor.ts frontend/src/views/Strategy.vue frontend/cypress/support/e2e.ts frontend/cypress/e2e/strategy_llm.cy.ts`

Expected: Diff contains only preview analysis changes and no credential, runtime-state, or unrelated formatting changes.

## Self-Review

- Spec coverage: The plan adds preview analysis without saving, displays the result, and saves/applies only after confirmation.
- Placeholder scan: No TBD/TODO/placeholder steps remain.
- Type consistency: Backend response reuses `LLMAnalyzeResponse`; frontend uses matching `LLMAnalyzeResponse` and `LLMPreviewAnalyzeRequest` names.
- Git note: The plan intentionally omits commit steps because this environment forbids committing unless the user explicitly asks.
