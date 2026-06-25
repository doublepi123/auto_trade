"""P217: Convex Risk Budgeting / Risk Parity (Maillard–Roncalli, Spinu).

Find long-only portfolio weights such that each asset's risk contribution
matches a target budget. The equal-risk-contribution (ERC) special case
distributes risk evenly across assets — the true "risk parity" portfolio.
Unlike HRP (P209, which is a heuristic top-down bisection), risk budgeting is
the *convex* optimization formulation whose solution provably satisfies the
risk-contribution equalities (Maillard, Roncalli, Teïletche 2010).

Spinu (2013) showed the ERC / risk-budgeting problem is the minimizer of the
strictly convex log-barrier objective

    f(y) = (1/2) yᵀ Σ y − Σ_i b_i · ln(y_i),   with w = y / Σy

whose first-order condition ``Σy_i − b_i/y_i = 0`` yields, after normalizing,
risk contributions ``RC_i(w) ∝ b_i``. We solve it via Newton iteration with a
backtracking line search (Hessian is always positive-definite from the
diagonal barrier term, so Newton is well-defined even when Σ is singular — a
key advantage over plain inversion). Pure Python, dict-based I/O, zero new
deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.platform.construction import PortfolioConstructionModel
from app.platform.covariance import ledoit_wolf_shrinkage, portfolio_variance

__all__ = [
    "risk_budgeting",
    "risk_contributions",
    "risk_budgeting_weights",
    "risk_budgeting_converged",
    "RiskBudgetingModel",
]


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve ``matrix · x = rhs`` via Gaussian elimination with partial pivoting."""
    n = len(matrix)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-15:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_val = aug[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col] / pivot_val
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]
    return [aug[i][n] / aug[i][i] for i in range(n)]


def risk_contributions(
    cov: dict[tuple[str, str], float], weights: dict[str, float]
) -> dict[str, float]:
    """Per-asset risk contribution ``RC_i = w_i · (Σw)_i`` (sums to portfolio variance)."""
    symbols = [s for s in weights if weights[s] != 0]
    if not symbols:
        return {}
    sigma_w = {
        s: sum(weights.get(i, 0.0) * cov.get((s, i), 0.0) for i in symbols)
        for s in symbols
    }
    return {s: weights[s] * sigma_w[s] for s in symbols}


def risk_budgeting(
    cov: dict[tuple[str, str], float],
    budgets: dict[str, float] | None = None,
    *,
    max_iter: int = 200,
    tol: float = 1e-10,
) -> dict[str, Any]:
    """Convex risk-budgeting weights via Spinu's Newton iteration.

    Returns ``{"weights", "risk_contributions", "relative_risk_contributions",
    "converged", "iterations"}``. Default budgets are equal (ERC). Zero-variance
    assets are dropped (with budget renormalization). NaN/inf or asymmetric cov
    entries raise ``ValueError``.
    """
    symbols = sorted({s for pair in cov for s in pair})
    if not symbols:
        return {"weights": {}, "risk_contributions": {}, "relative_risk_contributions": {},
                "converged": True, "iterations": 0}
    n = len(symbols)
    if n == 1:
        s = symbols[0]
        v = cov.get((s, s), 0.0)
        return {"weights": {s: 1.0}, "risk_contributions": {s: v},
                "relative_risk_contributions": {s: 1.0}, "converged": True, "iterations": 0}

    # Validate Σ, drop zero-variance assets.
    sigma: list[list[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            a, b = symbols[i], symbols[j]
            vij = cov.get((a, b), 0.0)
            vji = cov.get((b, a), 0.0)
            if not math.isfinite(vij) or not math.isfinite(vji):
                raise ValueError("non-finite cov entry")
            if abs(vij - vji) > 1e-9:
                raise ValueError("cov not symmetric")
            sigma[i][j] = vij
    keep = [i for i in range(n) if sigma[i][i] > 0]
    if not keep:
        # all-zero variance → equal weight
        w = {s: 1.0 / n for s in symbols}
        return {"weights": w, "risk_contributions": {s: 0.0 for s in symbols},
                "relative_risk_contributions": {s: 1.0 / n for s in symbols},
                "converged": True, "iterations": 0}
    if len(keep) == 1:
        idx = keep[0]
        w = {symbols[i]: (1.0 if i == idx else 0.0) for i in range(n)}
        return {"weights": w, "risk_contributions": {symbols[idx]: sigma[idx][idx]},
                "relative_risk_contributions": {symbols[idx]: 1.0}, "converged": True, "iterations": 0}

    keep_syms = [symbols[i] for i in keep]
    keep_sigma = [[sigma[i][j] for j in keep] for i in keep]
    k = len(keep)

    # budgets: default equal; align + validate.
    if budgets is None:
        b = [1.0 / k] * k
    else:
        b = []
        for s in keep_syms:
            if s not in budgets:
                raise ValueError(f"budget missing for symbol {s}")
            bv = float(budgets[s])
            if bv <= 0:
                raise ValueError(f"budget for {s} must be > 0")
            b.append(bv)
        bs = sum(b)
        if bs <= 0:
            raise ValueError("budgets must sum to a positive value")
        b = [v / bs for v in b]

    # Newton iteration on f(y) = 1/2 yᵀΣy − Σ b_i ln(y_i).
    y = [1.0] * k
    converged = False
    iters = 0
    for it in range(max_iter):
        iters = it + 1
        Sy = [sum(keep_sigma[i][j] * y[j] for j in range(k)) for i in range(k)]
        grad = [Sy[i] - b[i] / y[i] for i in range(k)]
        # Hessian: H_ij = Σ_ij (i≠j), H_ii = Σ_ii + b_i/y_i²
        H = [[keep_sigma[i][j] for j in range(k)] for i in range(k)]
        for i in range(k):
            H[i][i] += b[i] / (y[i] * y[i])
        dy = _solve(H, [-g for g in grad])
        if dy is None:
            # singular (shouldn't happen — barrier makes H PD); gradient step.
            dy = [-g * 1e-6 for g in grad]
        # backtracking line search keeping y > 0
        alpha = 1.0
        while alpha > 1e-12:
            new_y = [max(y[i] + alpha * dy[i], 1e-12) for i in range(k)]
            if all(new_y[i] > 0 for i in range(k)):
                break
            alpha *= 0.5
        y = new_y
        # convergence: gradient norm small
        gnorm = math.sqrt(sum(g * g for g in grad))
        if gnorm < tol:
            converged = True
            break

    total = sum(y)
    w_keep = {keep_syms[i]: y[i] / total for i in range(k)}
    # restore dropped symbols with weight 0
    weights = {s: w_keep.get(s, 0.0) for s in symbols}

    rc = risk_contributions(cov, weights)
    rc_sum = sum(rc.values())
    rel_rc = {s: (rc[s] / rc_sum if rc_sum > 0 else 0.0) for s in rc}
    return {
        "weights": weights,
        "risk_contributions": rc,
        "relative_risk_contributions": rel_rc,
        "converged": converged,
        "iterations": iters,
    }


def risk_budgeting_weights(
    returns: dict[str, list[float]] | None = None,
    cov: dict[tuple[str, str], float] | None = None,
    budgets: dict[str, float] | None = None,
) -> dict[str, float]:
    """Convenience: weights only (cov from Ledoit-Wolf shrinkage if returns given)."""
    if cov is None:
        if returns is None:
            return {}
        cov, _ = ledoit_wolf_shrinkage(returns)
    return risk_budgeting(cov, budgets)["weights"]


def risk_budgeting_converged(
    rc: dict[str, float], budgets: dict[str, float], tol: float = 1e-6
) -> bool:
    """Check whether relative risk contributions match the target budgets."""
    total = sum(rc.values())
    if total <= 0:
        return False
    bsum = sum(budgets.values()) or 1.0
    for s, b in budgets.items():
        rel = rc.get(s, 0.0) / total
        if abs(rel - b / bsum) > tol:
            return False
    return True


@dataclass(frozen=True)
class RiskBudgetingModel:
    """PortfolioConstructionModel that sizes via convex risk budgeting (ERC)."""

    returns_panel: dict[str, list[float]] | None = None
    cov: dict[tuple[str, str], float] | None = None
    budgets: dict[str, float] | None = None
    name: str = "risk_budgeting"

    def target_weights(
        self,
        signals: dict[str, Decimal],
        *,
        volatilities: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        active = [s for s, v in signals.items() if v != 0]
        if not active:
            return {}
        if self.cov is not None:
            cov_active = {(a, b): self.cov.get((a, b), 0.0) for a in active for b in active}
            w = risk_budgeting(cov_active, self.budgets)["weights"]
            return {s: Decimal(str(w.get(s, 0.0))) for s in active}
        if self.returns_panel is not None:
            panel = {s: self.returns_panel.get(s, []) for s in active}
            panel = {s: v for s, v in panel.items() if len(v) >= 2}
            if len(panel) >= 2:
                w = risk_budgeting_weights(returns=panel, budgets=self.budgets)
                return {s: Decimal(str(w.get(s, 0.0))) for s in active}
        ew = Decimal("1") / Decimal(len(active))
        return {s: ew for s in active}