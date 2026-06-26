# Fractional Differencing API 10-Iteration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the existing platform fractional-differencing feature engineering module through a validated backend API with small, testable usability enhancements.

**Architecture:** Keep the computation in `backend/app/platform/fractional_differencing.py` as pure Python. Add a thin FastAPI endpoint in `backend/app/platform/api.py` using existing dict-payload style for platform endpoints, returning JSON-friendly report data and actionable 422 errors.

**Tech Stack:** Python 3.11, FastAPI, pytest, Pydantic-free dict payloads matching current `platform/api.py` conventions.

---

### Scope: 10 autonomous feature iterations

1. Add a platform API endpoint `POST /api/platform/fractional-differencing`.
2. Accept `series`, `d`, and `threshold` payload fields.
3. Support `mode="ffd"` and `mode="expanding"`.
4. Return `output` with the same length as input.
5. Return `n_weights`, `n_output`, and `adf_stat` summary fields.
6. Return `first_valid_index` for UI chart alignment.
7. Return `weights` for explainability/debugging.
8. Validate empty series with HTTP 422.
9. Validate invalid `d`, `threshold`, and `mode` with HTTP 422.
10. Add API tests plus existing module tests to lock behavior.

### Files

- Modify: `backend/app/platform/api.py`
- Keep/add: `backend/app/platform/fractional_differencing.py`
- Keep/add: `backend/tests/platform/test_fractional_differencing.py`
- Modify: `backend/tests/test_platform_api.py`

### TDD execution steps

- [x] Write API tests in `backend/tests/test_platform_api.py` for FFD success, expanding success, and invalid payloads.
- [x] Run the new tests and confirm they fail with 404/422 before implementation.
- [x] Add the API route and minimal validation in `backend/app/platform/api.py`.
- [x] Run the API tests and existing fractional differencing tests.
- [x] Refactor only duplicated parsing/error handling introduced in this change.
- [x] Run targeted backend verification.

### Validation commands

```bash
cd backend && python3 -m pytest tests/platform/test_fractional_differencing.py tests/test_platform_api.py -v
cd backend && python3 -m basedpyright
```

### Self-review

- No placeholders: complete.
- Scope is intentionally backend-only and avoids unrelated frontend churn.
- Existing untracked fractional differencing module is treated as a feature module and not rewritten beyond necessary fixes.
