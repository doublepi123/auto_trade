"""Tests for P259 spectral analysis (naive DFT periodogram)."""

from __future__ import annotations

import math

import pytest

from app.platform.spectral_analysis import (
    SpectralAnalysisResult,
    periodogram,
    spectral_report,
)


def _square_wave(n_cycles: int = 4, period: int = 4) -> list[float]:
    """Build a length-``n_cycles * period`` square wave ``[0,1,0,-1]*k``."""
    base = [0.0, 1.0, 0.0, -1.0]
    return base * n_cycles


def test_periodogram_length_matches_input():
    series = [math.sin(i) for i in range(32)]
    pg = periodogram(series, sample_rate=16.0)
    assert len(pg) == 32


def test_periodogram_square_wave_dominant_bin():
    """A [0,1,0,-1]*4 series has one full cycle every 4 samples.

    At ``sample_rate = 16`` (16 samples / second), 4 samples per cycle ⇒ a
    physical frequency of ``16/4 = 4 Hz``. The dominant (non-DC) periodogram
    bin therefore must sit at ``k`` such that ``k * 16 / 16 = k`` is the
    closest integer to 4 (i.e. bin index 4).
    """
    series = _square_wave(n_cycles=4, period=4)
    pg = periodogram(series, sample_rate=16.0)
    # Drop the DC bin (k=0) before searching for the dominant peak.
    non_dc = pg[1:]
    dom_bin = max(range(len(non_dc)), key=lambda i: non_dc[i]) + 1
    # The dominant frequency derived from this bin is sample_rate*bin/N = 16*4/16 = 4.
    n = len(series)
    dom_freq = dom_bin * 16.0 / n
    assert abs(dom_freq - 4.0) < 1.0, f"dominant frequency {dom_freq} should be near 4 Hz"


def test_periodogram_dc_bin_is_zero_mean_signal_when_constant():
    # A constant signal has all energy in DC; non-DC bins ~ 0.
    series = [3.0] * 16
    pg = periodogram(series, sample_rate=1.0)
    assert pg[0] > 0.0
    for value in pg[1:]:
        assert value < 1e-9


def test_periodogram_invalid_sample_rate():
    with pytest.raises(ValueError):
        periodogram([1.0, 2.0, 3.0], sample_rate=0.0)
    with pytest.raises(ValueError):
        periodogram([1.0, 2.0, 3.0], sample_rate=-1.0)


def test_periodogram_empty_raises():
    with pytest.raises(ValueError):
        periodogram([])


def test_periodogram_non_finite_raises():
    with pytest.raises(ValueError):
        periodogram([1.0, float("nan"), 2.0])
    with pytest.raises(ValueError):
        periodogram([1.0, float("inf"), 2.0])


def test_periodogram_non_numeric_raises():
    with pytest.raises((TypeError, ValueError)):
        periodogram([1.0, "x", 2.0])  # type: ignore[list-item]


def test_spectral_report_returns_dominant_frequency():
    series = _square_wave(n_cycles=4, period=4)
    report = spectral_report(series, sample_rate=16.0)
    assert isinstance(report, SpectralAnalysisResult)
    # Dominant frequency of the 4-sample-period square wave at sr=16 ⇒ ~4 Hz.
    assert 3.0 <= report.dominant_frequency <= 5.0


def test_spectral_report_entropy_in_unit_interval():
    series = [math.sin(i / 2.0) + math.cos(i / 3.0) for i in range(64)]
    report = spectral_report(series, sample_rate=8.0)
    assert 0.0 <= report.spectral_entropy <= 1.0


def test_spectral_report_entropy_zero_for_pure_tone():
    # A pure cosine concentrated in a single non-DC bin ⇒ entropy ~0.
    n = 64
    series = [math.cos(2 * math.pi * 4 * i / n) for i in range(n)]
    report = spectral_report(series, sample_rate=64.0)
    assert report.spectral_entropy < 1e-6


def test_spectral_report_band_energy_shares_in_unit_interval():
    series = [math.sin(i / 2.0) + math.cos(i / 3.0) for i in range(64)]
    bands = [(0.0, 2.0), (2.0, 5.0), (5.0, 32.0)]
    report = spectral_report(series, sample_rate=64.0, bands=bands)
    assert len(report.band_energy) == len(bands)
    for share in report.band_energy:
        assert 0.0 <= share <= 1.0
    # Band shares should sum to roughly 1 (cover the entire positive band).
    assert abs(sum(report.band_energy) - 1.0) < 1e-6


def test_spectral_report_band_labels_match():
    series = [math.sin(i / 2.0) for i in range(64)]
    bands = [(0.0, 1.0), (1.0, 4.0)]
    report = spectral_report(series, sample_rate=64.0, bands=bands)
    assert report.band_labels == ["0.0-1.0", "1.0-4.0"]


def test_spectral_report_invalid_band_raises():
    series = [math.sin(i / 2.0) for i in range(32)]
    with pytest.raises(ValueError):
        spectral_report(series, sample_rate=8.0, bands=[(5.0, 1.0)])  # low > high
    with pytest.raises(ValueError):
        spectral_report(series, sample_rate=8.0, bands=[(-1.0, 1.0)])  # negative low


def test_spectral_report_empty_raises():
    with pytest.raises(ValueError):
        spectral_report([])


def test_spectral_report_to_dict_keys():
    series = _square_wave(n_cycles=4, period=4)
    report = spectral_report(series, sample_rate=16.0, bands=[(0.0, 2.0), (2.0, 8.0)])
    d = report.to_dict()
    for key in (
        "n",
        "sample_rate",
        "dominant_frequency",
        "dominant_bin",
        "spectral_entropy",
        "frequencies",
        "periodogram",
        "band_labels",
        "band_energy",
    ):
        assert key in d, f"missing field {key!r}"
    assert d["n"] == 16
    assert d["sample_rate"] == 16.0
    assert len(d["frequencies"]) == 16
    assert len(d["periodogram"]) == 16


def test_spectral_report_frozen_dataclass_is_immutable():
    series = _square_wave(n_cycles=4, period=4)
    report = spectral_report(series, sample_rate=16.0)
    with pytest.raises(Exception):
        report.dominant_frequency = 99.0  # type: ignore[misc]


def test_spectral_report_pure_tone_avoids_mirror_frequency():
    """P259 audit: a pure real tone at bin ``k`` must NOT select its mirror.

    For a length-``N`` real series the DFT is conjugate-symmetric: bin ``k``
    and bin ``N - k`` carry the same power. ``spectral_report`` must therefore
    select the dominant bin from the *independent positive half-spectrum*
    (``k = 1 .. N // 2``) only — picking the mirror image (e.g. ``N - k``)
    would report a nonsensical dominant frequency near the Nyquist limit.

    We exercise both a pure cosine and a pure sine at ``N = 64``, ``sr = 64``,
    ``k = 3``; either could expose the mirror-image tie-break because floating
    point round-off can make ``powers[3]`` and ``powers[61]`` differ by a hair.
    """
    n = 64
    sample_rate = 64.0
    tone_k = 3
    expected_freq = tone_k * sample_rate / n  # 3.0 Hz
    phases = [
        ("cos", math.cos),
        ("sin", math.sin),
    ]
    for label, trig in phases:
        series = [trig(2 * math.pi * tone_k * i / n) for i in range(n)]
        report = spectral_report(series, sample_rate=sample_rate)
        assert report.dominant_bin == tone_k, (
            f"{label} tone: dominant_bin {report.dominant_bin} should be {tone_k}, "
            f"not the mirror bin {n - tone_k}"
        )
        assert abs(report.dominant_frequency - expected_freq) < 1e-9, (
            f"{label} tone: dominant_frequency {report.dominant_frequency} "
            f"should be {expected_freq}"
        )
