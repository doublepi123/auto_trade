"""Confidence-shrunk adaptive Kelly position sizing.

Trade outcomes estimate the binary Kelly inputs introduced by Kelly (1956):
win probability, average positive payoff, and average loss magnitude. Because
plug-in Kelly estimates are especially aggressive with limited data, the
long-only fraction is multiplied by ``n / (n + k)``, where ``k`` is a
pseudo-sample strength (30 by default). This is the Buhlmann (1967)
effective-sample-size credibility weight: it shrinks an uncertain estimate
toward zero and approaches full credibility as evidence accumulates. Applying
that weight to Kelly sizing follows the conservative fractional-Kelly rationale
of MacLean, Thorp, and Ziemba (2011). Samples without both a win and a loss
cannot identify a payoff ratio, so they receive zero sizing and confidence.

Pure Python, deterministic, and free of I/O or external numerical libraries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.platform.kelly import kelly_binary

__all__ = ["AdaptiveSizingReport", "adaptive_kelly"]


@dataclass(frozen=True, slots=True)
class AdaptiveSizingReport:
    """Estimated binary edge and its confidence-shrunk Kelly sizing."""

    full_kelly: float
    shrunk_kelly: float
    shrink_factor: float
    win_prob: float
    avg_win: float
    avg_loss: float
    n_trades: int
    edge: float
    confidence: float

    def to_dict(self) -> dict[str, float | int]:
        """Return a JSON-compatible representation of the report."""
        return {
            "full_kelly": self.full_kelly,
            "shrunk_kelly": self.shrunk_kelly,
            "shrink_factor": self.shrink_factor,
            "win_prob": self.win_prob,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "n_trades": self.n_trades,
            "edge": self.edge,
            "confidence": self.confidence,
        }


def adaptive_kelly(
    outcomes: Sequence[float],
    shrinkage_strength: float = 30.0,
) -> AdaptiveSizingReport:
    """Estimate and shrink a long-only binary Kelly fraction from outcomes.

    Positive values are wins, negative values are losses, and zero values are
    neutral observations included in ``n_trades``. For a two-sided sample, the
    confidence and shrink factor are ``n / (n + shrinkage_strength)``.
    ``shrinkage_strength=0`` disables shrinkage.

    Raises:
        ValueError: If ``outcomes`` is empty or ``shrinkage_strength`` is
            negative.
    """
    n_trades = len(outcomes)
    if n_trades == 0:
        message = "outcomes must not be empty"
        raise ValueError(message)
    if shrinkage_strength < 0.0:
        message = "shrinkage_strength must be >= 0"
        raise ValueError(message)

    wins = [outcome for outcome in outcomes if outcome > 0.0]
    losses = [-outcome for outcome in outcomes if outcome < 0.0]
    win_prob = len(wins) / n_trades
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    edge = win_prob * avg_win - (1.0 - win_prob) * avg_loss

    if not wins or not losses:
        return AdaptiveSizingReport(
            full_kelly=0.0,
            shrunk_kelly=0.0,
            shrink_factor=0.0,
            win_prob=win_prob,
            avg_win=avg_win,
            avg_loss=avg_loss,
            n_trades=n_trades,
            edge=edge,
            confidence=0.0,
        )

    full_kelly = max(0.0, kelly_binary(win_prob, avg_win, avg_loss))
    shrink_factor = n_trades / (n_trades + shrinkage_strength)
    shrunk_kelly = max(0.0, full_kelly * shrink_factor)
    return AdaptiveSizingReport(
        full_kelly=full_kelly,
        shrunk_kelly=shrunk_kelly,
        shrink_factor=shrink_factor,
        win_prob=win_prob,
        avg_win=avg_win,
        avg_loss=avg_loss,
        n_trades=n_trades,
        edge=edge,
        confidence=shrink_factor,
    )
