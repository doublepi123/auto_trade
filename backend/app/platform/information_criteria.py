"""P380: Information criteria for model selection.

Computes AIC (Akaike), BIC (Bayesian/Schwarz), and HQIC (Hannan-Quinn)
for a set of candidate models. Reports per-model criteria and identifies
the best model by AIC and BIC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = ["InformationCriteriaResult", "information_criteria_report"]


@dataclass(frozen=True)
class InformationCriteriaResult:
    per_model: dict[str, dict[str, float]]
    best_aic_model: str
    best_bic_model: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_model": dict(self.per_model),
            "best_aic_model": self.best_aic_model,
            "best_bic_model": self.best_bic_model,
        }


def information_criteria_report(
    models: list[dict[str, Any]],
    *,
    n: int,
) -> InformationCriteriaResult:
    """Compute information criteria (AIC, BIC, HQIC) for a set of models.

    Args:
        models: List of dicts, each with keys:
            - ``name`` (str): Model identifier.
            - ``log_likelihood`` (float): Log-likelihood at optimum.
            - ``n_params`` (int): Number of free parameters.
        n: Number of observations (sample size).

    Returns:
        InformationCriteriaResult with per-model criteria and best models.

    Raises:
        ValueError: If inputs are invalid.
    """
    if not isinstance(models, list) or not models:
        raise ValueError("models must be a non-empty list")

    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError("n must be a positive int")
    if n < 1:
        raise ValueError("n must be >= 1")

    if n == 1:
        # ln(ln(1)) is undefined; use a small epsilon
        pass

    ln_n = math.log(n)
    ln_ln_n = math.log(ln_n) if ln_n > 0 else 0.0

    per_model: dict[str, dict[str, float]] = {}
    best_aic = float("inf")
    best_aic_name = ""
    best_bic = float("inf")
    best_bic_name = ""

    for i, model in enumerate(models):
        if not isinstance(model, dict):
            raise ValueError(f"models[{i}] must be a dict")
        if "name" not in model or "log_likelihood" not in model or "n_params" not in model:
            raise ValueError(
                f"models[{i}] must contain 'name', 'log_likelihood', 'n_params'"
            )

        name = str(model["name"])
        if isinstance(model["log_likelihood"], bool) or not isinstance(model["log_likelihood"], (int, float)):
            raise ValueError(f"models[{i}] log_likelihood must be numeric")
        if isinstance(model["n_params"], bool) or not isinstance(model["n_params"], int):
            raise ValueError(f"models[{i}] n_params must be an int")
        ll = float(model["log_likelihood"])
        k = int(model["n_params"])

        if k < 0:
            raise ValueError(
                f"models[{i}] n_params must be >= 0"
            )
        if not math.isfinite(ll):
            raise ValueError(
                f"models[{i}] log_likelihood must be finite"
            )

        aic = -2.0 * ll + 2.0 * k
        bic = -2.0 * ll + k * ln_n
        hqic = -2.0 * ll + 2.0 * k * ln_ln_n

        per_model[name] = {
            "aic": float(aic),
            "bic": float(bic),
            "hqic": float(hqic),
        }

        if aic < best_aic:
            best_aic = aic
            best_aic_name = name
        if bic < best_bic:
            best_bic = bic
            best_bic_name = name

    return InformationCriteriaResult(
        per_model=per_model,
        best_aic_model=best_aic_name,
        best_bic_model=best_bic_name,
    )
