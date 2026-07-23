from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_statistics_summary_endpoint_returns_report() -> None:
    response = client.post(
        "/api/platform/statistics-summary",
        json={"values": [1.0, 2.0, 3.0, 4.0]},
    )

    assert response.status_code == 200, response.text
    assert "mean" in response.json()


def test_statistics_summary_endpoint_rejects_empty_values() -> None:
    response = client.post("/api/platform/statistics-summary", json={"values": []})

    assert response.status_code == 422


def test_momentum_indicators_endpoint_returns_indicators() -> None:
    response = client.post(
        "/api/platform/momentum-indicators",
        json={
            "closes": [10.0, 11.0, 12.0, 11.0, 13.0, 14.0],
            "highs": [11.0, 12.0, 13.0, 12.0, 14.0, 15.0],
            "lows": [9.0, 10.0, 11.0, 10.0, 12.0, 13.0],
            "volumes": [100.0, 110.0, 120.0, 90.0, 130.0, 140.0],
            "fast": 2,
            "slow": 3,
            "signal": 2,
            "period": 3,
            "fastk": 3,
            "slowk": 2,
            "slowd": 2,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "macd" in body
    assert "stochastic" in body
    assert "obv" in body


def test_momentum_indicators_endpoint_rejects_empty_closes() -> None:
    response = client.post(
        "/api/platform/momentum-indicators",
        json={"closes": []},
    )

    assert response.status_code == 422


def test_vol_targeting_endpoint_returns_report() -> None:
    response = client.post(
        "/api/platform/vol-targeting",
        json={
            "returns": [0.01, -0.005, 0.007, -0.002],
            "target_vol": 0.1,
        },
    )

    assert response.status_code == 200, response.text
    assert "leverage" in response.json()


def test_vol_targeting_endpoint_requires_target_vol() -> None:
    response = client.post(
        "/api/platform/vol-targeting",
        json={"returns": [0.01, -0.005]},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("payload", "expected_key"),
    [
        (
            {
                "mode": "binary",
                "outcomes": [1, 0, 1, 1],
                "p0": 0.45,
                "p1": 0.6,
                "alpha": 0.05,
                "beta": 0.1,
            },
            "decision",
        ),
        (
            {
                "mode": "normal",
                "values": [0.1, 0.2, 0.15],
                "mu0": 0.0,
                "mu1": 0.1,
                "sigma": 0.2,
                "alpha": 0.05,
                "beta": 0.1,
            },
            "decision",
        ),
    ],
)
def test_sprt_endpoint_returns_report(
    payload: dict[str, str | float | list[int] | list[float]],
    expected_key: str,
) -> None:
    response = client.post("/api/platform/sprt", json=payload)

    assert response.status_code == 200, response.text
    assert expected_key in response.json()


def test_sprt_endpoint_requires_mode() -> None:
    response = client.post("/api/platform/sprt", json={})

    assert response.status_code == 422


def test_bocpd_endpoint_returns_report() -> None:
    response = client.post(
        "/api/platform/bocpd",
        json={"values": [0.0, 0.1, 3.0, 3.1]},
    )

    assert response.status_code == 200, response.text
    assert "n_observations" in response.json()


def test_bocpd_endpoint_rejects_empty_values() -> None:
    response = client.post("/api/platform/bocpd", json={"values": []})

    assert response.status_code == 422


def test_adaptive_sizing_endpoint_returns_report() -> None:
    response = client.post(
        "/api/platform/adaptive-sizing",
        json={"outcomes": [1.0, -0.5, 0.75, -0.25]},
    )

    assert response.status_code == 200, response.text
    assert "shrunk_kelly" in response.json()


def test_adaptive_sizing_endpoint_rejects_empty_outcomes() -> None:
    response = client.post("/api/platform/adaptive-sizing", json={"outcomes": []})

    assert response.status_code == 422


def test_cusum_endpoint_returns_report() -> None:
    response = client.post(
        "/api/platform/cusum",
        json={"values": [0.0, 0.0, 1.0, 1.0]},
    )

    assert response.status_code == 200, response.text
    assert "n_signals" in response.json()


def test_cusum_endpoint_rejects_empty_values() -> None:
    response = client.post("/api/platform/cusum", json={"values": []})

    assert response.status_code == 422
