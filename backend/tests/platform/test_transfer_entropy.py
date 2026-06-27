from __future__ import annotations

import pytest

from app.platform.transfer_entropy import transfer_entropy_report


def test_transfer_entropy_detects_information_flow():
    source = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    target = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    body = transfer_entropy_report(source, target, lag=1, bins=2).to_dict()
    assert body["forward_te"] > 0
    assert body["reverse_te"] >= 0
    assert "net_te" in body


def test_transfer_entropy_rejects_mismatched_length():
    with pytest.raises(ValueError):
        transfer_entropy_report([1, 2], [1], lag=1)
