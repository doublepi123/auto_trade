# backend/app/domain/analysis/

## Responsibility

Classical technical-analysis toolkit: a stateless `TechnicalIndicators` calculator and a `MarketStateDetector`. This is the indicator library rendered into LLM prompt context and reused by watchlist scoring.

## Design

- **`TechnicalIndicators`** (`technical_indicators.py`, 535 lines): static-method library, no instance state —
  - `calculate_rsi`, `calculate_macd`, `calculate_obv`, `calculate_adx`, `calculate_stochastic`, `calculate_cci`, `calculate_williams_r`, `calculate_vwap`
  - `analyze_volume` (returns a `VolumeAnalysis` TypedDict)
  - `aggregate_signals(indicator_results)` — combines per-indicator results into a consensus block
  - `analyze_multi_timeframe` — multi-window summary
  Every method returns `float | None` when the lookback window is insufficient; callers must tolerate `None` rather than expecting zeros.
- **`MarketStateDetector`** (`market_state.py`, 83 lines): `detect(...)` folds a price series into a `MarketState` value object (regime summary).
- Pure functions of input lists: no clock, no I/O, no settings, no state carried between calls.

## Flow

`services/data_aggregator.py` builds price/volume series from stored bars → calls `TechnicalIndicators` methods → results flow into the prompt `context` dict (rendered by `prompt/context_module.py` as the indicator blocks) and into watchlist quant scoring. `aggregate_signals` / `analyze_multi_timeframe` produce the consensus summaries the LLM actually reasons over; `MarketStateDetector` output feeds regime descriptions.

## Integration

- **Consumer**: `services/data_aggregator.py` (sole importer — both modules).
- **Downstream of results**: `prompt/context_module.py` indicator blocks; `FeatureSelector.parse_selection` validates the indicator names that came from these blocks; `prompt/sentiment_module.py` renders the sentiment counterpart.
- Package `__init__.py` is empty — import from the modules directly (`from app.domain.analysis.technical_indicators import TechnicalIndicators`).
- Tests need no fixtures: plain lists in, values out.
