"""P259: Spectral analysis — naive DFT periodogram, entropy, band energy.

Pure-Python frequency-domain diagnostics for a uniformly-sampled scalar series.
No numpy/scipy/pandas dependency; the discrete Fourier transform is computed
directly with ``math.cos`` / ``math.sin``.

Public surface
--------------

* **periodogram(series, sample_rate)** — naive DFT power spectrum (``|X_k|²``).
* **spectral_report(series, sample_rate, bands)** — frozen
  :class:`SpectralAnalysisResult` aggregating the dominant non-DC frequency,
  Shannon spectral entropy normalised to ``[0, 1]``, and per-band energy share.

Conventions
-----------

For a length-``N`` real input ``x_n`` the naive DFT is

    X_k = Σ_{n=0}^{N-1} x_n · exp(-2πi·k·n / N)

and the periodogram power at bin ``k`` is ``|X_k|²``. Physical frequencies
follow the usual ``f_k = k · sample_rate / N`` mapping. The DC bin (``k = 0``)
is excluded from the dominant-frequency and entropy calculations so that a
constant offset does not mask the cyclical content.

Band energy share is the fraction of *non-DC* spectral energy that falls
inside each ``(low, high]`` frequency band; the shares therefore sum to 1.0
when the bands partition the positive half-spectrum.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "SpectralAnalysisResult",
    "periodogram",
    "spectral_report",
]


_MAX_SERIES = 5000
"""Upper bound on input length, mirroring the platform's other numeric endpoints."""


def _validate_series(series: Sequence[float]) -> list[float]:
    """Coerce ``series`` to ``list[float]`` after validating each entry.

    Raises ``ValueError`` if the series is empty, too long, or contains a
    non-finite / non-numeric value. Raises ``TypeError`` for entries whose
    type cannot be coerced to ``float``.
    """
    if not isinstance(series, list):
        # Accept any sequence (e.g. tuple) by materialising a list first; a
        # non-iterable scalar surfaces as a TypeError here, which the caller's
        # ``except (TypeError, ValueError)`` block already handles.
        series = list(series)  # type: ignore[arg-type]
    if len(series) == 0:
        raise ValueError("series must be non-empty")
    if len(series) > _MAX_SERIES:
        raise ValueError(f"series must contain at most {_MAX_SERIES} values")
    coerced: list[float] = []
    for value in series:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("series entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("series entries must be finite numbers")
        coerced.append(number)
    return coerced


def periodogram(series: Sequence[float], sample_rate: float = 1.0) -> list[float]:
    """Naive DFT periodogram ``|X_k|²`` of ``series`` sampled at ``sample_rate``.

    Returns a list of length ``N`` (one power value per frequency bin ``k``);
    the bin-to-frequency mapping is ``f_k = k · sample_rate / N``. Raises
    ``ValueError`` for an empty / non-finite series or non-positive sample rate,
    and ``TypeError`` for non-numeric entries.
    """
    samples = _validate_series(series)
    if not math.isfinite(sample_rate) or sample_rate <= 0.0:
        raise ValueError("sample_rate must be a positive finite number")
    n = len(samples)
    powers: list[float] = []
    two_pi_over_n = 2.0 * math.pi / n
    for k in range(n):
        re = 0.0
        im = 0.0
        angle = two_pi_over_n * k
        for nn, sample in enumerate(samples):
            theta = angle * nn
            re += sample * math.cos(theta)
            im -= sample * math.sin(theta)
        powers.append(re * re + im * im)
    return powers


def _positive_half(powers: Sequence[float], frequencies: Sequence[float]) -> tuple[list[float], list[float]]:
    """Return the positive-frequency half of a real-input periodogram.

    For a length-``N`` real series the DFT is conjugate-symmetric: bins ``k``
    and ``N-k`` carry the same power. Only the independent positive bins
    ``k = 1 .. ⌊(N-1)/2⌋`` (plus the unique Nyquist bin ``N/2`` when ``N`` is
    even) carry non-redundant information, so we keep exactly those. Excluding
    the redundant mirror image keeps entropy normalisation and band-energy
    shares physically meaningful.
    """
    n = len(powers)
    if n <= 1:
        return [], []
    last = n // 2  # exclusive upper bound; for even N the Nyquist bin (k=N/2) is included.
    half_powers = [powers[k] for k in range(1, last + 1)]
    half_freqs = [frequencies[k] for k in range(1, last + 1)]
    return half_powers, half_freqs


def _shannon_entropy_normalised(half_powers: Sequence[float]) -> float:
    """Shannon entropy of the positive-frequency spectrum, normalised to ``[0, 1]``.

    A spectrum concentrated in a single bin yields entropy 0; a flat spectrum
    yields entropy 1. ``half_powers`` must already be the positive-frequency
    half (DC excluded, mirror image dropped).
    """
    total = sum(half_powers)
    if total <= 0.0:
        return 0.0
    n = len(half_powers)
    if n < 2:
        return 0.0
    entropy = 0.0
    for power in half_powers:
        if power <= 0.0:
            continue
        p = power / total
        entropy -= p * math.log(p)
    max_entropy = math.log(n)
    return entropy / max_entropy if max_entropy > 0.0 else 0.0


def _validate_bands(bands: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Validate each ``(low, high)`` band and return it coerced to floats.

    Bands must satisfy ``0 ≤ low < high``; ``high`` may exceed the Nyquist
    frequency (excess range simply captures no extra energy). Raises
    ``ValueError`` (or ``TypeError``) on malformed input.
    """
    if not isinstance(bands, list):
        raise TypeError("bands must be a list of (low, high) tuples")
    coerced: list[tuple[float, float]] = []
    for entry in bands:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise TypeError("each band must be a (low, high) pair")
        low_raw, high_raw = entry
        if isinstance(low_raw, bool) or isinstance(high_raw, bool) or \
                not isinstance(low_raw, (int, float)) or not isinstance(high_raw, (int, float)):
            raise TypeError("band bounds must be finite numbers")
        low = float(low_raw)
        high = float(high_raw)
        if not math.isfinite(low) or not math.isfinite(high):
            raise ValueError("band bounds must be finite numbers")
        if low < 0.0:
            raise ValueError("band low must be non-negative")
        if high <= low:
            raise ValueError("band high must be greater than low")
        coerced.append((low, high))
    return coerced


def _band_energy_shares(
    half_powers: Sequence[float],
    half_freqs: Sequence[float],
    bands: Sequence[tuple[float, float]],
) -> list[float]:
    """Fraction of positive-frequency spectral energy inside each ``(low, high]`` band.

    Bands that overlap still count each bin's energy only once per band; the
    returned shares therefore need not sum to 1.0 unless the bands partition
    the positive half-spectrum.
    """
    total = sum(half_powers)
    if total <= 0.0:
        return [0.0 for _ in bands]
    shares: list[float] = []
    for low, high in bands:
        energy = 0.0
        for power, freq in zip(half_powers, half_freqs):
            if low < freq <= high:
                energy += power
        shares.append(energy / total)
    return shares


@dataclass(frozen=True)
class SpectralAnalysisResult:
    """Aggregated spectral diagnostics for a uniformly-sampled series.

    * ``dominant_bin`` — index ``k > 0`` of the largest periodogram power.
    * ``dominant_frequency`` — physical frequency of the dominant bin.
    * ``spectral_entropy`` — Shannon entropy of the non-DC spectrum, normalised
      to ``[0, 1]``.
    * ``band_labels`` / ``band_energy`` — per-band energy share (empty when no
      ``bands`` were supplied).
    """

    n: int
    sample_rate: float
    dominant_bin: int
    dominant_frequency: float
    spectral_entropy: float
    frequencies: list[float]
    periodogram: list[float]
    band_labels: list[str]
    band_energy: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "sample_rate": self.sample_rate,
            "dominant_bin": self.dominant_bin,
            "dominant_frequency": self.dominant_frequency,
            "spectral_entropy": self.spectral_entropy,
            "frequencies": self.frequencies,
            "periodogram": self.periodogram,
            "band_labels": self.band_labels,
            "band_energy": self.band_energy,
        }


def spectral_report(
    series: Sequence[float],
    sample_rate: float = 1.0,
    bands: Sequence[tuple[float, float]] | None = None,
) -> SpectralAnalysisResult:
    """Run a naive periodogram and summarise the cyclical content of ``series``.

    Parameters
    ----------
    series:
        Non-empty list of finite numbers sampled at uniform spacing.
    sample_rate:
        Samples per unit time (must be positive). Frequencies are reported in
        the reciprocal unit (e.g. Hz when ``sample_rate`` is in Hz).
    bands:
        Optional list of ``(low, high)`` frequency bands; each share is the
        fraction of non-DC spectral energy falling in ``(low, high]``.

    Returns a :class:`SpectralAnalysisResult`. Raises ``ValueError`` /
    ``TypeError`` on invalid input — the platform endpoint converts these into
    HTTP 422 responses.
    """
    samples = _validate_series(series)
    if not math.isfinite(sample_rate) or sample_rate <= 0.0:
        raise ValueError("sample_rate must be a positive finite number")
    n = len(samples)
    powers = periodogram(samples, sample_rate=sample_rate)
    frequencies = [k * sample_rate / n for k in range(n)]

    # Entropy, band energy and dominant bin all use only the independent
    # positive-frequency half (``k = 1 .. ⌊N/2⌋``). For a real input the DFT
    # is conjugate-symmetric, so bins ``k`` and ``N - k`` carry identical
    # power; selecting the dominant bin over the full non-DC spectrum could
    # pick the redundant mirror image (e.g. ``N - k``) and report a frequency
    # near the Nyquist limit instead of the true tone. Restricting to the
    # positive half keeps the choice physically meaningful.
    half_powers, half_freqs = _positive_half(powers, frequencies)
    entropy = _shannon_entropy_normalised(half_powers)

    if half_powers:
        dom_index = max(range(len(half_powers)), key=lambda i: half_powers[i])
        # ``half_powers`` covers bins ``k = 1 .. ⌊N/2⌋`` in order, so the bin
        # index into ``half_powers`` maps to DFT bin ``dom_index + 1``.
        dom_bin = dom_index + 1
    else:
        dom_bin = 0
    dom_freq = frequencies[dom_bin] if dom_bin < n else 0.0

    if bands is not None:
        validated_bands = _validate_bands(bands)
        band_labels = [f"{low}-{high}" for low, high in validated_bands]
        band_energy = _band_energy_shares(half_powers, half_freqs, validated_bands)
    else:
        band_labels = []
        band_energy = []

    return SpectralAnalysisResult(
        n=n,
        sample_rate=sample_rate,
        dominant_bin=dom_bin,
        dominant_frequency=dom_freq,
        spectral_entropy=entropy,
        frequencies=frequencies,
        periodogram=powers,
        band_labels=band_labels,
        band_energy=band_energy,
    )
