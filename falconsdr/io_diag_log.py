"""Always-on, low-overhead diagnostics for SoapyBackend.read() - written
straight to a JSONL file (never stdout/print), so it can be inspected after
the fact with no FALCONSDR_DEBUG_LIVE_AUDIO env var and no shell redirection
needed - just restart the app and read the file back whenever.

Exists to chase two things at once:

  1. The ~14% audio-vs-wall-clock gap found by cross-checking WAV metadata
     (duration_sec) against actual recorded sample count - "multi_call_reads"
     in the summary record below is a direct regression check on the
     buffer-size fix for that (readStream() needing 2 calls per read()
     instead of 1 when the scratch buffer was smaller than the requested
     chunk) - should sit near 0 now; if it creeps back up, the fix isn't
     holding for some chunk size.

  2. A user-reported hunch: signals look artificially stronger (more false
     positives) when the CPU is busier (e.g. Whisper transcription running).
     Plausible mechanism: a delayed readStream() call risks the driver's own
     buffer overflowing (SOAPY_SDR_OVERFLOW and friends - all negative
     return codes), and our read() loop currently has no way to tell the
     caller "these samples aren't necessarily time-contiguous with the
     previous ones" - splicing across a silent gap looks like a phase
     discontinuity to the FFT, which spreads energy across many bins as if
     it were a real broadband signal. "overflow_count"/"overflow" records
     below exist to first confirm whether this actually happens, and how it
     correlates with system load, before deciding what (if anything) to do
     about it - measure first.

Two kinds of records, one JSON object per line:

  "overflow" - immediate, one per negative return code from readStream()
  (SOAPY_SDR_OVERFLOW=-4 is the one of interest, but any negative code is
  logged) - includes how many samples were already collected for the
  in-progress read() call, since a splice risk only exists once got > 0.

  "summary" - once every _SUMMARY_INTERVAL_S, aggregate stats for that
  window: how many read() calls needed >1 readStream() round-trip, how many
  overflow events happened, median/max read() latency, and the 1-minute
  load average as a cheap, dependency-free proxy for "how busy was the
  machine" (no psutil - this is diagnostic-only, not worth a new
  dependency) to eyeball against the other two.

Deliberately never touches the read()/scanning path itself beyond a few
float appends and an occasional file write - any failure to write is caught
and ignored here, same policy as calibration_log.py.
"""
from __future__ import annotations

import json
import os
import time

from .config import CONFIG_DIR

LOG_PATH = CONFIG_DIR / "sdr_io_diag.jsonl"
# Diagnostic-only, meant to be read back and discarded, not kept forever -
# same "quietly stop rather than fill the disk" policy as calibration_log.py,
# just a smaller cap since this is meant for a short, targeted capture.
_MAX_BYTES = 20 * 1024 * 1024
_SUMMARY_INTERVAL_S = 5.0


class SdrIoDiagLogger:
    def __init__(self) -> None:
        self._enabled = True
        self._window_start = time.monotonic()
        self._read_times: list[float] = []
        self._multi_call_reads = 0
        self._overflow_count = 0

    def _append(self, record: dict) -> None:
        if not self._enabled:
            return
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > _MAX_BYTES:
                self._enabled = False
                return
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def _loadavg1() -> float | None:
        # getloadavg() doesn't exist at all on Windows (AttributeError) -
        # this runs on the SDR acquisition thread, so it must never raise.
        try:
            return os.getloadavg()[0]
        except (OSError, AttributeError):
            return None

    def record_error(self, code: int, call_index: int, got_so_far: int, num_samples: int) -> None:
        """One negative-return readStream() call within an in-progress
        read(). Logged immediately (not batched into the summary) since
        these should be rare - each one is worth seeing on its own."""
        self._overflow_count += 1
        self._append({
            "t": "overflow", "ts": time.time(), "code": code,
            "call_index": call_index, "got_so_far": got_so_far,
            "num_samples": num_samples, "loadavg1": self._loadavg1(),
        })

    def record_read(self, elapsed_s: float, calls: int) -> None:
        """Called once per completed read() (not per readStream() call)."""
        self._read_times.append(elapsed_s)
        if calls > 1:
            self._multi_call_reads += 1
        now = time.monotonic()
        if now - self._window_start < _SUMMARY_INTERVAL_S:
            return
        times = sorted(self._read_times)
        n = len(times)
        self._append({
            "t": "summary", "ts": time.time(),
            "reads": n,
            "multi_call_reads": self._multi_call_reads,
            "overflow_count": self._overflow_count,
            "read_median_ms": round(times[n // 2] * 1000, 1) if n else None,
            "read_max_ms": round(times[-1] * 1000, 1) if n else None,
            "loadavg1": self._loadavg1(),
        })
        self._window_start = now
        self._read_times.clear()
        self._multi_call_reads = 0
        self._overflow_count = 0
