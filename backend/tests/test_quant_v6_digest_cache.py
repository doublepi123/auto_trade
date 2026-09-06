from dataclasses import replace
from datetime import date
from decimal import Decimal
from functools import partial

import pytest

from app.domain.watchlist_quant_v6 import semantics
from app.domain.watchlist_quant_v6.artifact import QuantV6ArtifactError


def _bars():
    return tuple(
        semantics.QuantV6Bar(
            start_at=start, open=Decimal("100"), high=Decimal("101"),
            low=Decimal("99"), close=Decimal("100"), volume=Decimal("1000"),
        )
        for start in semantics.quant_v6_expected_rth_bar_starts("US", date(2026, 7, 30))
    )


def test_repeated_session_digest_reuses_encoding_without_skipping_validation(monkeypatch):
    bars = _bars()
    calls = []
    original = semantics.quant_v6_payload_sha256

    def counted(payload):
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(semantics, "quant_v6_payload_sha256", counted)
    digest = partial(
        semantics.quant_v6_session_bars_sha256,
        symbol="CACHE.US", market="US", session_date=date(2026, 7, 30),
    )
    first = digest(bars=bars)
    assert digest(bars=list(bars)) == first
    assert len(calls) == 1
    changed = (replace(bars[0], close=Decimal("100.5")), *bars[1:])
    assert digest(bars=changed) != first
    assert len(calls) == 2
    with pytest.raises(semantics.QuantV6SemanticError):
        digest(bars=bars[:-1])
    # Equal Decimal values may differ in validity: never key on Decimal equality.
    invalid = replace(bars[0])
    object.__setattr__(invalid, "close", Decimal("100." + "0" * 10000))
    with pytest.raises(QuantV6ArtifactError):
        digest(bars=(invalid, *bars[1:]))


def test_session_digest_matches_original_canonical_payload():
    bars = _bars()
    payload = {
        "bar_minutes": semantics.QUANT_V6_BAR_MINUTES,
        "bars": [bar.canonical_payload() for bar in bars],
        "market": "US", "session_date": "2026-07-30", "symbol": "EXACT.US",
    }
    assert semantics.quant_v6_session_bars_sha256(
        symbol="EXACT.US", market="US", session_date=date(2026, 7, 30), bars=bars,
    ) == semantics.quant_v6_payload_sha256(payload)
