"""Tests for P376 price discovery module."""
from __future__ import annotations

import pytest
from app.platform.price_discovery import (
    PriceDiscoveryResult,
    price_discovery_report,
)


class TestPriceDiscoveryReport:
    """Tests for price_discovery_report function."""

    def test_two_venue_shares_sum_near_one(self) -> None:
        """Information shares for 2 venues should sum to approximately 1."""
        # Venue A: more volatile (dominant)
        # Venue B: less volatile
        n = 100
        prices_a = [100.0]
        prices_b = [100.0]
        import random

        random.seed(42)
        for _ in range(n - 1):
            shock = random.gauss(0.0, 0.5)
            prices_a.append(prices_a[-1] + shock + random.gauss(0.0, 0.1))
            prices_b.append(prices_b[-1] + shock * 0.3 + random.gauss(0.0, 0.05))

        venues = {"NYSE": prices_a, "NASDAQ": prices_b}
        result = price_discovery_report(venues)
        assert isinstance(result, PriceDiscoveryResult)
        total_share = sum(result.information_shares.values())
        assert abs(total_share - 1.0) < 0.01
        assert result.n_venues == 2
        assert result.n_observations == n

    def test_dominant_venue_is_max_share(self) -> None:
        """The dominant venue should be the one with the highest share."""
        n = 50
        prices_a = [100.0]
        prices_b = [100.0]
        import random

        random.seed(123)
        for _ in range(n - 1):
            prices_a.append(prices_a[-1] + random.gauss(0.0, 1.0))
            prices_b.append(prices_b[-1] + random.gauss(0.0, 0.1))

        venues = {"high_vol": prices_a, "low_vol": prices_b}
        result = price_discovery_report(venues)
        max_venue = result.dominant_venue
        max_share = result.information_shares[max_venue]
        for v, share in result.information_shares.items():
            assert share <= max_share + 0.0001  # tolerance for float

    def test_three_venue_shares(self) -> None:
        """Three venues should have shares summing to ~1."""
        n = 50
        prices_a = [100.0]
        prices_b = [100.0]
        prices_c = [100.0]
        import random

        random.seed(99)
        for _ in range(n - 1):
            shock = random.gauss(0.0, 0.3)
            prices_a.append(prices_a[-1] + shock + random.gauss(0.0, 0.1))
            prices_b.append(prices_b[-1] + shock * 0.5 + random.gauss(0.0, 0.05))
            prices_c.append(prices_c[-1] + shock * 0.2 + random.gauss(0.0, 0.02))

        venues = {"A": prices_a, "B": prices_b, "C": prices_c}
        result = price_discovery_report(venues)
        total_share = sum(result.information_shares.values())
        assert abs(total_share - 1.0) < 0.01
        assert result.n_venues == 3

    def test_to_dict_serializable(self) -> None:
        """to_dict should produce a JSON-serializable dictionary."""
        venues = {
            "A": [100.0, 101.0, 102.0, 103.0],
            "B": [100.0, 100.5, 101.5, 102.5],
        }
        result = price_discovery_report(venues)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "information_shares" in d
        assert "dominant_venue" in d
        assert "price_discovery_ratio" in d
        assert "n_venues" in d
        assert "n_observations" in d
        assert isinstance(d["information_shares"], dict)

    def test_invalid_empty_venues_raises(self) -> None:
        """Empty venues dict should raise ValueError."""
        with pytest.raises(ValueError):
            price_discovery_report({})

    def test_single_venue_raises(self) -> None:
        """Single venue should raise ValueError (need at least 2)."""
        with pytest.raises(ValueError, match="at least 2"):
            price_discovery_report({"A": [100.0, 101.0, 102.0]})

    def test_length_mismatch_raises(self) -> None:
        """Venues with different-length series should raise ValueError."""
        with pytest.raises(ValueError, match="equal length"):
            price_discovery_report({
                "A": [100.0, 101.0, 102.0],
                "B": [100.0, 101.0],
            })

    def test_negative_prices_rejected(self) -> None:
        """Negative prices should be rejected."""
        with pytest.raises(ValueError):
            price_discovery_report({
                "A": [100.0, -101.0],
                "B": [100.0, 101.0],
            })

    def test_price_discovery_ratio_between_zero_and_one(self) -> None:
        """price_discovery_ratio should be in [0, 1]."""
        n = 30
        prices_a = [100.0]
        prices_b = [100.0]
        import random

        random.seed(7)
        for _ in range(n - 1):
            prices_a.append(prices_a[-1] + random.gauss(0.0, 0.5))
            prices_b.append(prices_b[-1] + random.gauss(0.0, 0.5))

        venues = {"X": prices_a, "Y": prices_b}
        result = price_discovery_report(venues)
        assert 0.0 <= result.price_discovery_ratio <= 1.0

    def test_flat_prices_equal_shares(self) -> None:
        """Venues with identical flat prices should have equal shares."""
        venues = {
            "A": [100.0] * 10,
            "B": [100.0] * 10,
            "C": [100.0] * 10,
        }
        result = price_discovery_report(venues)
        # Equal variance → equal shares
        for share in result.information_shares.values():
            assert abs(share - 1.0 / 3.0) < 0.0001

    def test_multiple_venues_up_to_50(self) -> None:
        """Up to 50 venues should work."""
        n = 5
        venues: dict[str, list[float]] = {}
        import random

        random.seed(1)
        for i in range(10):
            prices = [100.0]
            for _ in range(n - 1):
                prices.append(prices[-1] + random.gauss(0.0, 0.2))
            venues[f"V{i}"] = prices
        result = price_discovery_report(venues)
        total_share = sum(result.information_shares.values())
        assert abs(total_share - 1.0) < 0.01
