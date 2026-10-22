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
pooled across every window this has ever run under: the physical cause is
the tuner's own IF response relative to wherever it's currently tuned, not
a property of any one frequency, so a bucket's data from one band's window
is just as valid evidence for another band's window at the same offset.

Keyed by ``gain_mode`` ("auto"/hardware, "sw_auto"/software, "manual") -
NOT pooled across those: real, user-reported case - switching between
hardware and software AGC (gotcha 27) measurably shifts the *absolute*
level by several to ~10dB (hardware AGC can run hotter/closer to
overdriving), and a curve built from a mix of both ends up under- or over-
correcting for whichever mode is actually active, which looked exactly
like "edge roll-off randomly got worse" after a session of switching modes
back and forth to compare them. Still pooled across different *gain_db*
values within the same mode (differential roll-off shape should be fairly
gain-independent physically - the real risk was always the qualitative
hardware-vs-software difference, not fine gain variation within one mode -
this hasn't been verified further, but splitting by exact gain_db as well
would fragment an already-small sample count for comparatively little
expected benefit).

Incremental cache (_CACHE_PATH): re-parsing the *entire* raw log from
scratch on every single scanner start doesn't scale - measured for real on
this project's own 50MB-capped power_calibration.jsonl (98909 lines, the
log's own size cap - see calibration_log.py's _MAX_BYTES): 1.8 seconds of
blocking work, every single Start click, forever (the log only grows over
a session's lifetime, it never shrinks). That's 1.8s of avoidable startup
latency sitting right at the exact moment (menu right after the SDR device
opens) a since-diagnosed startup crash has been observed - not confirmed as
the crash's actual cause, but real, measured, unnecessary work regardless,
worth cutting either way. Instead of re-reading the whole file, the running
per-bucket sums/counts and how many bytes of the log have already been
folded into them are persisted here, and each load only parses whatever
*new* bytes were appended since the last run (typically a few hundred KB
from one scanning session, milliseconds to parse) rather than the whole
thing - mathematically identical result (same running sums/counts, same
final curve), just without redoing already-done work every time.
"""
from __future__ import annotations

import json

import numpy as np

from .calibration_log import LOG_PATH
from .config import CONFIG_DIR

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

_CACHE_PATH = CONFIG_DIR / "edge_compensation_cache.json"


def _bucket_key(gain_mode: str, offset: int) -> str:
    return f"{gain_mode}|{offset}"


def _read_cache() -> tuple[dict[str, float], dict[str, int], int]:
    """Returns (sums, counts, processed_bytes), keyed by _bucket_key(gain_mode,
    offset); all empty/zero if there's no cache yet (first run ever), it's
    unreadable/corrupt, or it's still the old pre-gain_mode cache format (a
    plain int-keyed dict) - safe to treat exactly like "haven't processed
    anything yet" and rebuild from the raw log, never a crash."""
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        sums = {str(k): v for k, v in raw["sums"].items()}
        counts = {str(k): v for k, v in raw["counts"].items()}
        if sums and not all("|" in k for k in sums):
            # Old pre-gain_mode cache format (plain offset-int keys) - can't
            # tell which gain_mode those sums came from, so there's nothing
            # safe to salvage. Forces a one-time full re-read of the raw log
            # below to rebuild cleanly in the new format.
            return {}, {}, 0
        return sums, counts, int(raw["processed_bytes"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return {}, {}, 0


def _write_cache(sums: dict[str, float], counts: dict[str, int], processed_bytes: int) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps({
            "sums": sums,
            "counts": counts,
            "processed_bytes": processed_bytes,
        }), encoding="utf-8")
    except OSError:
        pass  # diagnostic cache only - never worth failing scanner startup over


def _load_floor_curve() -> dict[str, dict[int, float]]:
    """gain_mode -> {offset-from-window-center (Hz, 100kHz buckets) ->
    average measured floor dBFS}, averaged in the linear power domain (dB
    values don't average correctly) - separately per gain_mode (see module
    docstring for why). See the module docstring for why this only parses
    newly-appended bytes, not the whole log, on every call after the
    first."""
    sums, counts, processed_bytes = _read_cache()

    try:
        log_size = LOG_PATH.stat().st_size
    except OSError:
        log_size = 0  # no log yet (fresh install) - nothing to add to whatever the cache already has

    if log_size < processed_bytes:
        # The log shrank since the cache was built - cleared by hand, or a
        # cache left over from a different/older log. The remembered byte
        # offset is meaningless against a smaller file; start over rather
        # than silently skip real data or seek past EOF.
        sums, counts, processed_bytes = {}, {}, 0

    if log_size > processed_bytes:
        try:
            with LOG_PATH.open("rb") as f:
                f.seek(processed_bytes)
                new_data = f.read()
        except OSError:
            new_data = b""
        if new_data:
            # Only count up to the last newline as "processed" - a live
            # CalibrationLogger could be mid-write on the trailing line;
            # leaving it unconsumed here means it's picked up whole (not
            # truncated/corrupted) on the next load instead.
            last_nl = new_data.rfind(b"\n")
            if last_nl != -1:
                # Decode as text *before* handing lines to json.loads() -
                # loads() auto-detects encoding when given raw bytes (its
                # BOM-sniffing heuristic is for arbitrary JSON streams, not
                # guaranteed-UTF-8 log lines) and can misdetect a plain
                # UTF-8 line as UTF-16/32, corrupting perfectly valid JSON.
                # CalibrationLogger always writes UTF-8 (see calibration_log.py),
                # so decode explicitly instead of trusting that heuristic.
                for line in new_data[:last_nl + 1].decode("utf-8", errors="replace").splitlines():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("t") != "floor":
                        continue
                    gain_mode = rec.get("gain_mode") or "unknown"
                    for off_str, dbfs in rec.get("buckets", {}).items():
                        key = _bucket_key(gain_mode, int(off_str))
                        power = 10.0 ** (dbfs / 10.0)
                        sums[key] = sums.get(key, 0.0) + power
                        counts[key] = counts.get(key, 0) + 1
                processed_bytes += last_nl + 1
        _write_cache(sums, counts, processed_bytes)

    by_mode: dict[str, dict[int, float]] = {}
    for key in sums:
        if counts[key] < _MIN_SAMPLES_PER_BUCKET:
            continue
        gain_mode, off_str = key.split("|", 1)
        by_mode.setdefault(gain_mode, {})[int(off_str)] = 10.0 * np.log10(max(sums[key] / counts[key], 1e-12))
    return by_mode


class EdgeCompensation:
    def __init__(self, gain_mode: str = "") -> None:
        """``gain_mode`` is this session's active DeviceConfig.gain_mode -
        only that regime's own calibration history is used (see module
        docstring: hardware vs software AGC shift the absolute floor level
        enough to otherwise under/over-correct). A regime with no history
        yet (including a brand new install) gets 0dB correction everywhere,
        same as always - never a regression, just ungraded until that
        specific regime accumulates its own data."""
        curve = _load_floor_curve().get(gain_mode or "unknown", {})
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
