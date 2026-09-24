# backend/app/domain/prompt/

> See also [prompt/AGENTS.md](AGENTS.md) for conventions and anti-patterns in detail.

## Responsibility

LLM prompt plugin architecture: assembles the DeepSeek/MiniMax advisor prompt from composable `PromptModule` subclasses, and closes the loop with `FeatureSelector` for adaptive indicator gating. Pure string rendering — no I/O, no model calls, no settings access.

## Design

### Plugin architecture

- **`PromptModule` ABC** (`base.py`, 13 lines): the single public contract — abstract `render(context: dict[str, Any]) -> str`. A real `abc.ABC` with `@abstractmethod`, not a `Protocol`: registration is explicit in `PromptBuilder` rather than structural.
- **`PromptBuilder`** (`prompt_builder.py`, 24 lines): the only orchestrator. `add_module()` + `build(context)`; constructs module instances per `build()` call (no module-level globals, keeping renders stateless) and calls `render()` in a **fixed order: System → Context → Strategy → Selection → Sentiment → Output**. Reordering breaks the LLM's parsing pattern.
- **Context dict as the only coupling**: modules never reference each other; all data (bars, indicators, position, settings-derived values) arrives via the shared `context` dict. Modules must be pure functions of context — network/DB/clock injection is a layering violation.

### Adaptive indicator gating (two-way)

`SelectionModule` tells the LLM which indicators it may use → the LLM returns `selected_indicators` in its JSON response → `FeatureSelector.parse_selection()` (tolerant of empty/partial/unknown keys; falls back to the suggested set silently, never raises) → `filter_context()` prunes the *next* interval's context so only chosen indicators render. This keeps prompts small without dropping the LLM's own tool choices.

### Module-by-module notes

| File | Lines | Role |
|---|---|---|
| `base.py` | 13 | `PromptModule` ABC |
| `prompt_builder.py` | 24 | Fixed-order orchestrator |
| `system_module.py` | 21 | Quant role/rules — Chinese prose, English JSON keys |
| `context_module.py` | 217 | All data rendering (details below) |
| `strategy_module.py` | 48 | Current position, risk guard, tracked average cost |
| `selection_module.py` | 51 | Tells the LLM which indicators to use |
| `sentiment_module.py` | 23 | `MarketSentimentAnalyzer` output block |
| `output_module.py` | 46 | LLM-facing JSON output schema specification |
| `feature_selector.py` | 123 | Selection parsing + context filtering |

`context_module.py` owns all data rendering: daily and minute K-line tables (`_render_daily_table` / `_render_minute_table`), indicator blocks, position cost, and sentiment values. It is the only file >100 lines — it tolerates roughly a third of the directory's LOC, and new indicator blocks belong there, not in new modules. It guards `prompt_price <= 0` (pinned by an A4.2 test), short-circuiting before the DeepSeek call. Bilingual prompt text is hardcoded as f-strings — one Chinese string per logical block, no I18N layer; English is reserved for JSON keys.

### Hard rules (from AGENTS.md)

- Never import `app.services.*` here — pass data in via the context dict; keeps modules testable without env or fixtures.
- Never read `settings.deepseek_*` (or any settings) inside a module — configuration arrives through context.
- Never hardcode market-specific strings (`.US` / `.HK`) or tick sizes — render from data.
- Never bypass `PromptBuilder` by calling `module.render()` from a service — the builder owns ordering and future feature gating.
- Never raise on unknown `selected_indicators` keys in `feature_selector.py` — fall back to the suggested set silently.
- Never store module instances as module-level globals — `PromptBuilder` constructs them per `build()` call.

## Flow

1. `llm_advisor_service` gathers context (K-line rows, indicator values, strategy state, sentiment result) into a dict — all I/O (fetching bars, reading settings, model transport) happens here, before any module runs.
2. `PromptBuilder.build(context)` constructs fresh module instances and concatenates the six renders in fixed order → the prompt string (stored as the `prompt` column of the `llm_interactions` row).
3. The model responds; `FeatureSelector.parse_selection` extracts `selected_indicators` from the response (scraping JSON objects out of free-form text via `_json_object_candidates`) and `filter_context` prunes the next interval's context, so the loop is: suggest → select → narrow → suggest.
4. The advisor's `_parse_response` validates the reply against the schema `output_module.py` specified; parse failures degrade to "no recommendation", never an exception in the trading loop.

Adding a module is a 2-step change — new file subclassing `PromptModule`, plus one line at the right position in `PromptBuilder.build()`; skipping step 2 leaves the module unreachable.

## Integration

- **Consumer**: `services/llm_advisor_service.py` — the only caller of `PromptBuilder`; it owns the network call and `_parse_response` (which consumes the schema `output_module.py` specifies). Failures during `build()` itself are not caught locally; they rely on the advisor's outer try/except.
- **Upstream data**: outputs of `analysis/technical_indicators.py` and `sentiment/market_sentiment.py` are rendered through `context_module` / `sentiment_module`.
- **A/B experiments**: prompt variants are managed by `domain/experiment/ab_test_manager.py` (`PromptVersion` rows).
- Conventions: Chinese prose / English JSON keys; no market-specific strings (`.US`/`.HK`) hardcoded — render from data; never call `module.render()` directly from a service (the builder owns ordering and feature gating).
- History: P9 (commit `8168da2`) introduced the plugin architecture; P11 added `FeatureSelector` + `SelectionModule` for adaptive gating.
- Tests: `tests/test_prompt_modules.py` — pure functions, no fixtures.
