"""Additive dB correction for edge roll-off, built from real data
accumulated by CalibrationLogger's "floor" snapshots (see calibration_log.py)
- lets trigger_level_dbfs stay a single number while still giving a
frequency near a capture window's edge (weaker purely from tuner/IF roll-off,
not because anything is actually different about the transmission) roughly
the same effective sensitivity as one near the center.

Read once at scanner startup (roll-off is a stable physical property of the
tuner, not something that needs updating mid-session) from whatever
power_calibration.jsonl already has - the more real listening time behind
it, the better the curve. With no data yet (a fresh install, or a log that
got cleared) every offset defaults to 0 dB correction, identical to
pre-compensation behaviour - never a regression, just ungraded until data
accumulates.

Deliberately axed on offset-from-window-center (not absolute frequency) and
pooled across every window/gain this has ever run under: the physical cause
is the tuner's own IF response relative to wherever it's currently tuned,
not a property of any one frequency, so a bucket's data from one band's
window is just as valid evidence for another band's window at the same
offset. Not (yet) gain-aware - the real data this shipped with only ever
saw one steady auto-gain value across a 12+ hour session, so there was
nothing to differentiate by; if you regularly run wildly different manual
gain settings, the curve pools across them, which may be slightly less
accurate than a per-gain curve would be (differential roll-off shape should
be fairly gain-independent physically, but this hasn't been verified).
"""
from __future__ import annotations

import json

import numpy as np

from .calibration_log import LOG_PATH

_BUCKET_HZ = 100_000.0
# Buckets with fewer real samples than this are too noisy to trust - fall
# back to 0 dB (no correction) for that offset rather than risk overcorrecting
# off a handful of readings.
_MIN_SAMPLES_PER_BUCKET = 20
# Sanity clamp - real roll-off within the *usable* capture window (the
# already-cropped 90% USABLE_FRACTION region) has measured a few dB in
# practice, never dozens; caps a correction from ever masking a genuinely
# dead/misconfigured bucket as "just needs a bigger boost".
_MAX_CORRECTION_DB = 10.0


def _load_floor_curve() -> dict[int, float]:
    """Offset-from-window-center (Hz, 100kHz buckets) -> average measured
    floor dBFS across every "floor" record ever logged, averaged in the
    linear power domain (dB values don't average correctly)."""
    if not LOG_PATH.exists():
        return {}
    sums: dict[int, float] = {}
    counts: dict[int, int] = {}
    try:
        with LOG_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("t") != "floor":
                    continue
                for off_str, dbfs in rec.get("buckets", {}).items():
                    off = int(off_str)
                    power = 10.0 ** (dbfs / 10.0)
                    sums[off] = sums.get(off, 0.0) + power
                    counts[off] = counts.get(off, 0) + 1
    except OSError:
        return {}
    return {
        off: 10.0 * np.log10(max(sums[off] / counts[off], 1e-12))
        for off in sums if counts[off] >= _MIN_SAMPLES_PER_BUCKET
    }


class EdgeCompensation:
    def __init__(self) -> None:
        curve = _load_floor_curve()
        if curve:
            reference = max(curve.values())  # the window's own strongest bucket, not assumed to be exactly offset 0
            self._correction = {
                off: min(_MAX_CORRECTION_DB, max(0.0, reference - db))
                for off, db in curve.items()
            }
        else:
            self._correction = {}

    @property
    def has_data(self) -> bool:
        return bool(self._correction)

    @property
    def bucket_count(self) -> int:
        return len(self._correction)

    def correction_db(self, offset_hz: float) -> float:
        if not self._correction:
            return 0.0
        bucket = int(round(offset_hz / _BUCKET_HZ) * _BUCKET_HZ)
        return self._correction.get(bucket, 0.0)
