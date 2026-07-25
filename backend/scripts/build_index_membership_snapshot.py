#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import yaml


_N100_COMMIT = "9a23023b59707c5372ae1fff4ed983b3ad025c74"
_DOW_COMMIT = "650596e3c59a19d9c8767c8b504e3728da0fd07f"
_N100_BASE_URL = (
    "https://raw.githubusercontent.com/jmccarrell/n100tickers/"
    f"{_N100_COMMIT}/src/nasdaq_100_ticker_history"
)
_DOW_URL = (
    "https://raw.githubusercontent.com/unliftedq/index-constitution/"
    f"{_DOW_COMMIT}/history/dow30.csv"
)
_N100_SHA256 = {
    2022: "6313b84f4d178b8d37300a18109ad4f9551268b19e2b7b4383b7a5f742e18392",
    2023: "7d545922ec54325c9fc97eef738750d502ef4fd0bce3a58232741319febc7a3e",
    2024: "12bec472989760c3915e32582d42772f73241f4ef0067633ff57cb22cb3ae693",
    2025: "e20a547ed1bb5d6fde305e91560ed74aa9b74d6cadd7aee78dab8867b19366ee",
    2026: "f45d2b464ca7e52c81527385da88a455575414e7ec12786f52f0e70b255bed6b",
}
_DOW_SHA256 = (
    "42e8ec9910caf9db26e8f944fce3cc460b567f1862a0f3141b06b6f851975056"
)
_EFFECTIVE_START = date(2022, 1, 1)
_CATALOG_SNAPSHOT_DATE = date(2026, 7, 24)
_SNAPSHOT_ONLY_NASDAQ_SYMBOLS = ("HONA", "SPCX")
_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "domain"
    / "universe_selection"
    / "data"
    / "index_membership_history.json"
)


def _download(url: str, expected_sha256: str) -> bytes:
    with urlopen(url, timeout=30) as response:
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(
            f"source checksum mismatch for {url}: {actual}"
        )
    return payload


def _nasdaq_intervals() -> dict[str, list[list[str]]]:
    yearly: dict[int, dict[str, Any]] = {}
    for year, checksum in _N100_SHA256.items():
        payload = _download(
            f"{_N100_BASE_URL}/n100-ticker-changes-{year}.yaml",
            checksum,
        )
        # BaseLoader keeps ticker-like YAML scalars such as "ON" as
        # strings instead of applying YAML 1.1 boolean coercion.
        decoded = yaml.load(payload, Loader=yaml.BaseLoader)
        if not isinstance(decoded, dict):
            raise RuntimeError(f"invalid Nasdaq history for {year}")
        yearly[year] = decoded

    current = set(yearly[2022]["tickers_on_Jan_1"])
    opened = {
        symbol: _EFFECTIVE_START
        for symbol in current
    }
    intervals: dict[str, list[list[str]]] = {}
    for year in sorted(yearly):
        initial = set(yearly[year]["tickers_on_Jan_1"])
        if initial != current:
            raise RuntimeError(
                f"Nasdaq year boundary mismatch for {year}"
            )
        raw_changes = yearly[year].get("changes", {})
        if not isinstance(raw_changes, dict):
            raise RuntimeError(
                f"invalid Nasdaq changes for {year}"
            )
        for raw_change_date in sorted(raw_changes):
            change_date = date.fromisoformat(
                str(raw_change_date)
            )
            change = raw_changes[raw_change_date]
            if not isinstance(change, dict):
                raise RuntimeError(
                    f"invalid Nasdaq change on {change_date}"
                )
            for symbol in change.get("difference", []):
                start = opened.pop(symbol, None)
                if start is None:
                    raise RuntimeError(
                        f"Nasdaq removal without membership: {symbol}"
                    )
                intervals.setdefault(symbol, []).append(
                    [start.isoformat(), change_date.isoformat()]
                )
                current.remove(symbol)
            for symbol in change.get("union", []):
                if symbol in opened:
                    raise RuntimeError(
                        f"duplicate Nasdaq addition: {symbol}"
                    )
                opened[symbol] = change_date
                current.add(symbol)

    for symbol, start in opened.items():
        intervals.setdefault(symbol, []).append(
            [start.isoformat(), ""]
        )
    return {
        symbol: sorted(rows)
        for symbol, rows in sorted(intervals.items())
    }


def _dow_intervals() -> dict[str, list[list[str]]]:
    payload = _download(_DOW_URL, _DOW_SHA256)
    text = payload.decode("utf-8-sig")
    intervals: dict[str, list[list[str]]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        end = row["opt-out"]
        if end and date.fromisoformat(end) <= _EFFECTIVE_START:
            continue
        intervals.setdefault(row["symbol"], []).append(
            [row["opt-in"], end]
        )
    return {
        symbol: sorted(rows)
        for symbol, rows in sorted(intervals.items())
    }


def main() -> int:
    payload = {
        "source_version": (
            f"n100tickers-{_N100_COMMIT[:8]}_"
            f"index-constitution-{_DOW_COMMIT[:8]}_"
            f"catalog-snapshot-{_CATALOG_SNAPSHOT_DATE.isoformat()}"
        ),
        "effective_start_date": _EFFECTIVE_START.isoformat(),
        "catalog_snapshot_date": (
            _CATALOG_SNAPSHOT_DATE.isoformat()
        ),
        "sources": [
            {
                "name": "jmccarrell/n100tickers",
                "commit": _N100_COMMIT,
                "url": (
                    "https://github.com/jmccarrell/n100tickers/"
                    f"tree/{_N100_COMMIT}"
                ),
                "license": "MIT",
            },
            {
                "name": "unliftedq/index-constitution",
                "commit": _DOW_COMMIT,
                "url": (
                    "https://github.com/unliftedq/"
                    f"index-constitution/tree/{_DOW_COMMIT}"
                ),
                "license": "MIT",
            },
        ],
        "intervals": {
            "NASDAQ_100": _nasdaq_intervals(),
            "DJIA": _dow_intervals(),
        },
        "snapshot_overrides": {
            "NASDAQ_100": {
                symbol: _CATALOG_SNAPSHOT_DATE.isoformat()
                for symbol in _SNAPSHOT_ONLY_NASDAQ_SYMBOLS
            },
            "DJIA": {},
        },
    }
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(
        json.dumps(
            payload,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
