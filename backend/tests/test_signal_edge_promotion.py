from __future__ import annotations

import ast
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.domain.strategy_v2.signal_edge import (
    assess_first_passage,
    assess_signal_edge,
    clustered_t_test,
)


@pytest.mark.parametrize("days", [28, 60])
def test_analysis_pass_requires_separate_promotion_evidence(days: int) -> None:
    # Given: a positive cohort with 180 resolved brackets and strong first passage.
    first_passage = assess_first_passage(
        target_hits=150, stop_hits=30, stop_pct=1.0, target_pct=1.0,
    )
    net = clustered_t_test([
        (date(2026, 1, 1) + timedelta(days=i % days), 0.5 + (i % 7) * 0.1)
        for i in range(180)
    ])

    # When: analysis uses its unchanged default evidence floors.
    result = assess_signal_edge(first_passage=first_passage, clustered=net)

    # Then: analysis PASS is not permission to promote.
    assert result.verdict == "PASS"
    payload = asdict(result)
    assert "promotion" in payload, "analysis PASS must expose a separate promotion gate"
    promotion = payload["promotion"]
    assert promotion["net_significant"] is True
    assert promotion["first_passage_beats_baseline"] is True
    assert promotion["distinct_days"] == days
    assert promotion["resolved_brackets"] == 180
    assert promotion["required_distinct_days"] == 60
    assert promotion["required_resolved_brackets"] == 180
    assert promotion["sample_size_met"] is (days >= 60)
    assert promotion["eligible"] is False
    assert promotion["deflated_sharpe_distinguishable"] is False
    reasons = promotion["reasons"]
    assert "deflated Sharpe not yet computed" in reasons[-1]
    if days < 60:
        assert "28 trading days" in reasons[0]
        assert "60" in reasons[0]
    else:
        assert len(reasons) == 1


def test_promotion_fails_closed_when_statistics_are_unavailable() -> None:
    # Given: an empty cohort.
    first_passage = assess_first_passage(
        target_hits=0, stop_hits=0, stop_pct=1.0, target_pct=1.0,
    )
    # When: it is assessed.
    result = assess_signal_edge(
        first_passage=first_passage, clustered=clustered_t_test([]),
    )
    # Then: all four failed ANDs are disclosed in preregistration order.
    promotion = asdict(result)["promotion"]
    assert promotion["eligible"] is False
    assert [reason.split(":")[0] for reason in promotion["reasons"]] == [
        "AND #1", "AND #2", "AND #3", "AND #4",
    ]


def test_signal_edge_domain_imports_remain_pure() -> None:
    # Given: the domain files implementing signal assessment.
    root = Path(__file__).resolve().parents[1] / "app/domain/strategy_v2"
    # When: imports are parsed structurally.
    imports = [
        name
        for path in (root / "signal_edge.py", root / "futility.py", root / "clustered_returns.py")
        for node in ast.walk(ast.parse(path.read_text()))
        for name in (
            [node.module or ""] if isinstance(node, ast.ImportFrom)
            else [alias.name for alias in node.names] if isinstance(node, ast.Import)
            else []
        )
    ]
    # Then: no upward or I/O dependency is introduced.
    assert not any(name.startswith((
        "app.services", "app.platform", "app.api", "app.database", "app.models",
        "sqlalchemy", "sqlite3", "httpx", "requests",
    )) for name in imports)
