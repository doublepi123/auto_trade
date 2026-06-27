"""P313: Drawdown surface – joint depth × duration distribution.

Extracts every drawdown episode from an equity curve (peak → trough →
recovery), bins the (max_depth, duration) pairs, and returns a joint
distribution matrix plus the episode list.

Reference: typical drawdown surface analysis in systematic strategies.
Pure-Python — no scipy, no NumPy, no RNG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_series

__all__ = ["DrawdownSurfaceResult", "drawdown_surface_report"]


@dataclass(frozen=True)
class DrawdownSurfaceResult:
    num_episodes: int
    depth_bins: list[float]
    duration_bins: list[float]
    joint_matrix: list[list[float]]
    episodes: list[dict[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "num_episodes": self.num_episodes,
            "depth_bins": self.depth_bins,
            "duration_bins": self.duration_bins,
            "joint_matrix": self.joint_matrix,
            "episodes": self.episodes,
        }


def _extract_episodes(equity: list[float]) -> list[dict[str, float]]:
    """Extract all drawdown episodes (peak → trough → recovery)."""
    n = len(equity)
    if n < 2:
        return []
    episodes: list[dict[str, float]] = []
    peak = equity[0]
    peak_idx = 0
    in_drawdown = False
    trough = equity[0]
    trough_idx = 0

    for i in range(1, n):
        current = equity[i]
        if current >= peak:
            # New peak — if we were in a drawdown, close the episode
            if in_drawdown:
                max_depth = (peak - trough) / peak if peak > 0 else 0.0
                duration = trough_idx - peak_idx
                episodes.append({
                    "peak_value": peak,
                    "peak_index": float(peak_idx),
                    "trough_value": trough,
                    "trough_index": float(trough_idx),
                    "max_depth": max_depth,
                    "duration": float(duration),
                })
                in_drawdown = False
            peak = current
            peak_idx = i
        else:
            if not in_drawdown:
                in_drawdown = True
                trough = current
                trough_idx = i
            elif current < trough:
                trough = current
                trough_idx = i

    # If still in drawdown at end, record the episode
    if in_drawdown:
        max_depth = (peak - trough) / peak if peak > 0 else 0.0
        duration = trough_idx - peak_idx
        episodes.append({
            "peak_value": peak,
            "peak_index": float(peak_idx),
            "trough_value": trough,
            "trough_index": float(trough_idx),
            "max_depth": max_depth,
            "duration": float(duration),
        })

    return episodes


def drawdown_surface_report(
    equity_curve: list[float],
    *,
    depth_bins: int = 5,
    duration_bins: int = 5,
) -> DrawdownSurfaceResult:
    """Compute drawdown depth × duration joint distribution.

    Args:
        equity_curve: Sequence of equity values (non-empty, all finite).
        depth_bins: Number of bins for max drawdown depth dimension.
        duration_bins: Number of bins for drawdown duration dimension.

    Returns:
        DrawdownSurfaceResult with joint matrix, episodes, and bin edges.
    """
    if not isinstance(equity_curve, (list, tuple)):
        raise ValueError("equity_curve must be a list of finite numbers")
    if len(equity_curve) == 0:
        return DrawdownSurfaceResult(
            num_episodes=0,
            depth_bins=[],
            duration_bins=[],
            joint_matrix=[],
            episodes=[],
        )
    curve = validate_series(equity_curve, name="equity_curve", min_len=1)
    if len(curve) < 2:
        return DrawdownSurfaceResult(
            num_episodes=0,
            depth_bins=[],
            duration_bins=[],
            joint_matrix=[],
            episodes=[],
        )

    episodes = _extract_episodes(curve)

    if not episodes:
        return DrawdownSurfaceResult(
            num_episodes=0,
            depth_bins=[],
            duration_bins=[],
            joint_matrix=[],
            episodes=[],
        )

    depths = [ep["max_depth"] for ep in episodes]
    durations = [ep["duration"] for ep in episodes]

    if isinstance(depth_bins, bool) or not isinstance(depth_bins, int) or depth_bins < 2:
        raise ValueError("depth_bins must be an integer >= 2")
    if isinstance(duration_bins, bool) or not isinstance(duration_bins, int) or duration_bins < 2:
        raise ValueError("duration_bins must be an integer >= 2")

    min_depth = min(depths)
    max_depth = max(depths)
    depth_range = max_depth - min_depth
    if depth_range < 1e-12:
        depth_range = 1.0  # avoid zero-width bins

    min_dur = min(durations)
    max_dur = max(durations)
    dur_range = max_dur - min_dur
    if dur_range < 1e-12:
        dur_range = 1.0

    # Create bin edges
    depth_edges = [min_depth + i * depth_range / depth_bins for i in range(depth_bins + 1)]
    dur_edges = [min_dur + i * dur_range / duration_bins for i in range(duration_bins + 1)]

    # Fill joint matrix
    matrix: list[list[float]] = [[0.0] * duration_bins for _ in range(depth_bins)]
    for ep in episodes:
        d = ep["max_depth"]
        t = ep["duration"]
        di = min(depth_bins - 1, max(0, int((d - min_depth) / depth_range * depth_bins)) if depth_range > 0 else 0)
        ti = min(duration_bins - 1, max(0, int((t - min_dur) / dur_range * duration_bins)) if dur_range > 0 else 0)
        matrix[di][ti] += 1.0

    return DrawdownSurfaceResult(
        num_episodes=len(episodes),
        depth_bins=depth_edges,
        duration_bins=dur_edges,
        joint_matrix=matrix,
        episodes=episodes,
    )
