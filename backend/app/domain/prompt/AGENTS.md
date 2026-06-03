# `backend/app/domain/prompt/` — LLM Prompt Plugin Architecture

## OVERVIEW
LLM prompt assembled from 6 composable `PromptModule` subclasses + `PromptBuilder` orchestrator + `FeatureSelector` for adaptive indicator gating. Pure computation, no I/O.

## STRUCTURE
```
prompt/
├── base.py              # 13 lines  — PromptModule ABC, abstract render(context) -> str
├── prompt_builder.py    # 24 lines  — Orchestrator: calls module.render() in fixed order
├── system_module.py     # 20 lines  — Role / rules (quant role, bilingual Chinese prompt)
├── context_module.py    # 217 lines — K-line tables, indicator blocks, position cost, sentiment
├── strategy_module.py   # 48 lines  — Current position, risk guard, tracked avg
├── selection_module.py  # 49 lines  — Tells LLM which indicators to use
├── sentiment_module.py  # 23 lines  — MarketSentimentAnalyzer output block
├── output_module.py     # 44 lines  — JSON output schema spec
├── feature_selector.py  # 82 lines  — Parses LLM-returned selected_indicators, filters context
└── __init__.py          # empty
```

## WHERE TO LOOK
| Task | File | Notes |
|------|------|-------|
| Add new module type | `base.py` | Subclass `PromptModule`, implement `render(context: dict[str, Any]) -> str` |
| Add new technical indicator | `context_module.py` | Renders in dedicated block; `FeatureSelector.parse_selection` validates |
| Change output JSON schema | `output_module.py` | LLM-facing schema spec; `_parse_response` in `llm_advisor_service` consumes |
| Reorder module assembly | `prompt_builder.py` | `build()` calls modules in fixed sequence — order matters for prompt coherence |
| Adaptive indicator gating | `feature_selector.py` + `selection_module.py` | Two-way: LLM returns list → `FeatureSelector` filters next call's `context_module` |
| Change quant role/rules | `system_module.py` | Pure Chinese prompt; English reserved for JSON keys |

## CONVENTIONS
- **`render(context: dict[str, Any]) -> str`** is the only public contract; context is the shared dict passed through `PromptBuilder.build()`.
- **No I/O**: modules must be pure functions of context. Network/DB/clock injection is a layering violation — move to `services/`.
- **Chinese for prose, English for JSON keys**: `system_module` is bilingual (quant role Chinese + English JSON schema). `context_module` mixes Chinese labels with English data keys.
- **Order of assembly is fixed** in `PromptBuilder.build()`: System → Context → Strategy → Selection → Sentiment → Output. Reordering breaks the LLM's parsing pattern.
- **`FeatureSelector.parse_selection`** is the only entry that mutates the LLM response payload before the advisor consumes it; tolerate empty / partial / unknown keys without raising.

## ANTI-PATTERNS (THIS DIR)
- ❌ Importing from `app.services.*` — breaks the no-I/O contract. Use `context: dict` to pass data in.
- ❌ Calling `settings.deepseek_*` directly — pass via context dict; keeps modules testable without env.
- ❌ Hardcoding market-specific strings (`.US` / `.HK`) — keep symbols and tick sizes out of the prompt layer; render from data.
- ❌ Bypassing `PromptBuilder` by calling `module.render()` directly in `llm_advisor_service` — the builder owns ordering and future feature gating.
- ❌ Raising on unknown `selected_indicators` keys in `feature_selector.py` — fall back to default set silently.
- ❌ Storing module instances as module-level globals — `PromptBuilder` constructs them per `build()` call.

## UNIQUE STYLES
- **`ABC` over `Protocol`**: `PromptModule` is a real `abc.ABC` with `@abstractmethod`, not a `Protocol` — concrete registration in `PromptBuilder` rather than structural typing.
- **Context dict as the only coupling**: modules never reference each other; `PromptBuilder` is the only orchestrator that knows the assembly order.
- **`context_module.py` is the only file >100 lines** — it owns all data rendering and tolerates a 3rd of the dir's LOC. New indicator blocks go there, not in new modules.
- **Bilingual prompt hardcoded as f-strings** in `system_module.py` / `context_module.py` — no I18N layer. Keep one Chinese string per logical block, not fragmented translations.

## COMMANDS
```bash
# All modules are pure functions — unit tests need no fixtures
cd backend && python3 -m pytest tests/test_prompt_modules.py -v

# Type check
cd backend && python3 -m basedpyright app/domain/prompt/

# Adding a new module: 1 file + 1 line in prompt_builder.py
# 1. Create new_module.py subclassing PromptModule, implement render()
# 2. In PromptBuilder.build(), append self.new_module.render(context) at the right position
```

## NOTES
- **P9** (commit `8168da2`) introduced the plugin architecture; **P11** added `FeatureSelector` and `SelectionModule` for adaptive gating.
- Adding a new module is a 2-step change (file + 1 line in `PromptBuilder`); do not skip step 2 or the new module is unreachable.
- `context_module.py` line 391–393 already guards `prompt_price <= 0` (short-circuits before DeepSeek call) — A4.2 test pins this.
- The `llm_interactions` row's `prompt` column is the post-builder string; failures during `build()` itself are not currently caught (relies on `llm_advisor_service` outer try/except).
