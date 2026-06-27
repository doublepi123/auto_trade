from __future__ import annotations

from app.platform.portfolio_constraints import portfolio_constraints_report


def test_portfolio_constraints_reports_violations():
    body = portfolio_constraints_report(
        {"A": 0.7, "B": 0.3},
        prev_weights={"A": 0.5, "B": 0.5},
        groups={"A": "tech", "B": "finance"},
        adv={"A": 1000.0, "B": 1000.0},
        nav=1000.0,
        constraints={"max_position_weight": 0.6, "max_turnover": 0.1, "max_adv_participation": 0.5},
    ).to_dict()
    assert body["passed"] is False
    assert {item["constraint"] for item in body["violations"]} >= {"max_position_weight", "max_turnover", "max_adv_participation"}


def test_portfolio_constraints_passes_clean_portfolio():
    body = portfolio_constraints_report({"A": 0.5, "B": 0.5}, constraints={"max_position_weight": 0.6}).to_dict()
    assert body["passed"] is True
    assert body["violations"] == []


def test_portfolio_constraints_rejects_bad_groups_nav_and_limits():
    import pytest

    with pytest.raises(ValueError):
        portfolio_constraints_report({"A": 0.5}, groups="bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        portfolio_constraints_report({"A": 0.5}, nav=0)
    with pytest.raises(ValueError):
        portfolio_constraints_report({"A": 0.5}, constraints={"max_position_weight": -1.0})
    with pytest.raises(ValueError):
        portfolio_constraints_report({"A": float("nan")})
    with pytest.raises(ValueError):
        portfolio_constraints_report({"A": 0.5}, constraints={"max_position_weight": float("inf")})
