# backend/app/domain/sentiment/

## Responsibility

Derives a market-sentiment score from price-change series — the advisory sentiment block shown to the LLM interval advisor.

## Design

| File | Lines | Role |
|---|---|---|
| `market_sentiment.py` | 58 | `SentimentResult` TypedDict + `MarketSentimentAnalyzer` |

- **`MarketSentimentAnalyzer`** exposes a single public method, `analyze_from_price_changes(price_changes: list[float]) -> SentimentResult`. Pure threshold logic over the input list — no windowing state, no I/O, no clock.
- **`SentimentResult`** is a `TypedDict` (score + label), not a dataclass — matching the loose dict style the prompt context layer expects, so the result can be dropped straight into the context dict without conversion.
- Thresholds and labels are the only constants; there is deliberately no model call, no news source, no NLP — "sentiment" here is strictly price-derived.

## Flow

`services/data_aggregator.py` computes per-symbol price changes → `analyze_from_price_changes` classifies them → the `SentimentResult` dict lands in the prompt `context` → `prompt/sentiment_module.py` (`SentimentModule`) renders it as one labeled block in the assembled prompt.

## Integration

- **Consumer**: `services/data_aggregator.py` (sole importer).
- **Downstream**: `prompt/sentiment_module.py` block; indicator context comes from the sibling `analysis/` package.
- Advisory only: the sentiment score informs the LLM's recommendation; the LLM can never place live orders (P0), and sentiment never gates execution directly.
