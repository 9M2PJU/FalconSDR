"""Live audio monitoring output (speaker), backed directly by PortAudio -
through PyAudio when installed, else sounddevice (see HAVE_SOUNDDEVICE).

Degrades gracefully to a no-op if neither is installed, so the rest of
the app (scanning + recording) still works without live audio.

History: this went through several redesigns chasing a quality gap between
live monitoring and Odtwarzacz's recording playback - including, for a
while, routing live audio through Qt Multimedia (QMediaPlayer/GStreamer) so
it would use the exact same playback engine as recordings. That genuinely
matched Odtwarzacz's quality when it worked, but on real hardware it would
intermittently go completely silent mid-scan - after the first transmission,
with subsequent ones recording to disk correctly but never being heard live -
and every bit of state Qt itself exposes (mediaStatus, playbackState,
position, error) kept reporting perfectly healthy, continuous playback the
whole time. Whatever actually failed was buried below anything the app can
see or influence, somewhere in GStreamer's own audio sink / PipeWire
interaction - undiagnosable and unfixable from here without native
GStreamer debug tooling and a lot more patience than is reasonable to keep
asking for.

So: back to directly owning a PortAudio stream and pushing PCM to it
ourselves - one less layer (no container/demux/decode pipeline in the way,
no OS media framework deciding on its own when a stream idles out) - in
exchange for a small, known, bounded trade-off (PortAudio's own resampling
is a bit rougher than Qt Multimedia's, addressed below) instead of an
unbounded "sometimes it just stops" one.

Three things used to make live monitoring sound clearly worse than the very
same audio played back from a recording, all fixed here:

1. The demodulator runs at a deliberately low `AUDIO_RATE` (12500 Hz - see
   scanner_engine.py) since that's plenty for VHF voice and keeps the DSP
   cheap. No soundcard supports that rate natively though, so opening a
   PortAudio stream at 12500 Hz would make ALSA/PortAudio silently insert
   its own rate conversion - typically a cheap/low-quality one - to get up
   to the device's real 44100/48000 Hz. Fix: resample ourselves with
   scipy's polyphase resample_poly (same tool already used for IQ
   decimation in dsp.py) and open the PortAudio stream at the device's own
   native rate instead.

2. write() is fed one ~30 ms scanner chunk at a time (see CHUNK_SECONDS in
   scanner_engine.py), and resample_poly has no notion of a continuous
   stream - each call treats its input as self-contained and implicitly
   zero-pads past its end. Prepending real history fixed the *start* of
   each chunk's filter edge, but the *end* of every chunk was still being
   filtered against phantom zeros instead of the real audio about to
   follow, i.e. a small glitch every ~30 ms. Fix: hold back a small margin
   of not-yet-resampled samples at the tail of every call instead of
   resampling immediately, so every sample gets resampled with genuine
   audio as context on *both* sides once it's actually played.

3. The scanner thread doesn't just produce audio - every chunk it also
   demodulates (and, for however many channels are simultaneously active -
   see _Slot in scanner_engine.py - does that once per channel), optionally
   runs spectral-gate noise reduction, and used to also do a full FFT over
   the raw IQ for the live spectrum view every single tick; that FFT is now
   off this thread entirely (see _SpectrumWorker in scanner_engine.py), but
   the rest still isn't, and never fully can be - producing the audio *is*
   the demod step, it can't be deferred without deferring the audio itself.
   If any of that momentarily takes longer than the chunk's real-time
   duration (GIL contention, several channels active at once, OS scheduling
   jitter, ...), calling PortAudio's blocking stream.write() straight from
   that same thread means PortAudio's own buffer can starve and underrun
   right along with it - an audible crackle/glitch that has nothing to do
   with the *content* of the audio (a recording is never subject to a
   real-time deadline, so it never shows this). Fix, in two parts:
   a) don't call the blocking write() from the busy scanner thread at all -
      push resampled audio into a small ring buffer instead (cheap,
      non-blocking) and let PortAudio pull from it via its own callback, on
      its own dedicated thread, whenever *it* needs more - fully decoupled
      from whatever the scanner thread happens to be busy doing;
   b) that alone still means a *momentary* production hiccup reaches the
      speaker as a gap the instant the ring buffer empties out. So the
      callback also holds real playback back until the buffer has built up
      a small cushion (_PRIME_SECONDS) before actually starting/resuming -
      a brief hiccup then eats into that cushion instead of immediately
      being audible, at the cost of that same small, fixed amount of extra
      latency whenever playback starts or has to recover from a real
      underrun. Nothing about this changes what's written to a recording -
      the recorder (see scanner_engine.py's _play_and_record_slot) writes
      independently of this class entirely.

4. Even with all of the above, a long listening session would still slowly
   degrade - fine at first, progressively glitchier as it went on. Captured-
   and-analyzed live audio (see _CAPTURE_PATH) showed why: the ring buffer's
   occupancy wasn't just noisy, it was *trending down* over many seconds
   after every top-up. That's the signature of a genuine, persistent rate
   mismatch, not jitter - the SDR's sample clock and the sound card's own
   hardware playback clock are two independent real oscillators that are
   never *exactly* identical, even both being nominally "correct" (the same
   reason GStreamer's own audiobasesink logged "correct clock skew" back
   when live audio still went through it). resample_poly's ratio is exact
   sample-count math - if production is producing samples fractionally
   slower, in real time, than its nominal rate assumes, the resampled
   output represents fractionally less real time than the sound card (which
   *is* precisely clocked) consumes it in, forever, until corrected. A
   bigger cushion (point 3b) only delays hitting bottom, it can't prevent
   it. Fix: a small proportional controller (_correct_drift) compares the
   ring buffer's level against its target (_prime_len) on every write() and
   nudges this chunk's sample count by a handful of samples via linear
   interpolation (_retime) - stretching it fractionally if the buffer's
   running low, compressing it if it's running high - correcting only a
   tiny fraction of the error per call so the correction itself is
   inaudible, converging gradually instead of ever needing one jarring
   jump. Purely a live-monitoring output-stage nudge - it runs on the
   already-resampled audio right before it reaches the ring buffer, nothing
   upstream of this (including what the recorder writes) is touched by it.

5. Even with 1-4 above, real-hardware testing still found the ring buffer's
   *consumer* side (PortAudio's own callback thread, running _next_frames())
   at plain SCHED_OTHER priority, the same class of bug AGENTS.md gotcha 11
   found and fixed for the SDR acquisition thread: this callback is Python
   code, so it needs the GIL like any other thread in the process, and a
   SCHED_FIFO thread nearby (the acquisition thread) can keep winning the
   race for it with no fairness guarantee - except here a single missed
   callback is an *instant* audible glitch (PortAudio needs the next block
   *now*, unlike a retune which just waits a bit longer), not merely a
   slower sweep. Fix: _boost_priority_once() requests the same SCHED_FIFO
   priority class for this thread too, the first time the callback actually
   runs on it (can't be done at __init__ time like the acquisition thread -
   PortAudio, not this class, creates and owns this thread, so the first
   callback invocation is the earliest point code can run on it at all).
   Best-effort, same as the acquisition thread's own version - a missing
   CAP_SYS_NICE/rtprio limit just leaves it at normal priority.

The output stream is always opened with 2 channels: mono audio (AM/NFM/WFM
without stereo) is simply duplicated to L=R, and true stereo audio (WFM with
"Stereo" enabled, see dsp.py) is passed straight through - so the stream
never needs reopening when a channel's mono/stereo-ness changes mid-session.
Everything below operates on (n, 2) float32 arrays uniformly.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

try:
    import pyaudio
    HAVE_PYAUDIO = True
except ImportError:
    HAVE_PYAUDIO = False

# Fallback when PyAudio isn't available - the usual case on Windows/macOS,
# where PyAudio has no wheel for newer Pythons and needs a C compiler (plus
# PortAudio from brew on macOS). sounddevice's wheels bundle PortAudio
# itself. Same PortAudio underneath, so the same device indices, rates and
# callback model - only the Python wrapper differs.
try:
    import sounddevice
    HAVE_SOUNDDEVICE = True
except Exception:  # noqa: BLE001 - OSError if the bundled PortAudio fails to load
    HAVE_SOUNDDEVICE = False

# Same flag as the rest of the live-audio debugging effort (see
# scanner_engine.py's _DEBUG_TIMING) - when set, also captures every sample
# actually handed to PortAudio (silence included) to ~/smartscanatc_live_capture.wav
# on close(), so "what does live monitoring actually sound like" can be
# inspected/compared against a normal recording of the same transmission
# after the fact, instead of relying on it being caught by ear in the
# moment. Purely diagnostic - delete the file when done with it.
#
# Pre-allocated (see AudioOutput._capture), not a growing list appended to
# every callback - a first version did that, and a several-minutes-long
# capture session turned out to measurably slow the callback down more and
# more as the list grew (visible as the *captured audio itself* getting
# glitchier over the course of a long session, independent of - and on top
# of - whatever the real underlying issue was, actively corrupting exactly
# the "does it get worse over time" measurement this exists to make).
_DEBUG = os.environ.get("SMARTSCAN_DEBUG_LIVE_AUDIO") == "1"
_CAPTURE_PATH = Path.home() / "smartscanatc_live_capture.wav"
_CAPTURE_MAX_SECONDS = 300.0


def _dbg(msg: str) -> None:
    if _DEBUG:
        print(f"[audio-output {time.strftime('%H:%M:%S')}] {msg}", flush=True)

# Upper bound on how much resampled audio can pile up in the ring buffer
# before write() starts dropping the oldest of it - guards against unbounded
# memory growth if the output device ever stalls outright, without ever
# blocking the calling (scanner) thread.
_MAX_BUFFER_SECONDS = 2.0

# A fixed, moderate buffer size instead of leaving it to PortAudio's own
# auto-selection (frames_per_buffer=0) - a few real-world setups pick a
# surprisingly small or large default depending on host API, either of
# which can mean more frequent callback wakeups (more chances for jitter)
# or unnecessarily high output latency. 1024 frames is a well-worn middle
# ground for speech-rate audio at typical 44.1/48 kHz output rates.
_FRAMES_PER_BUFFER = 1024

# How much of a cushion the ring buffer builds up before the callback starts
# actually playing from it (on session start, and again any time it's
# genuinely run dry) - see point 3b in the module docstring. 0.2s wasn't
# enough in practice - captured-and-analyzed live audio (see _CAPTURE_PATH)
# showed the buffer level slowly bleeding back down from the cushion over a
# few seconds after every (re-)prime, occasionally reaching zero for a
# single callback (~20-40ms, audible as a small periodic "hiccup") well
# before the next real underrun would have re-armed priming. 0.5s gave
# roughly 4x fewer of those in testing - not zero (see _REPRIME_STREAK's
# docstring: some of this is a structural production/consumption cadence
# mismatch that a bigger cushion alone doesn't fully erase, just delays),
# but a real, measured improvement for a modest extra startup delay.
_PRIME_SECONDS = 0.5

# How many *consecutive* empty callbacks it takes before re-priming kicks in
# again after session start. write() (~30ms cadence, paced by the scanner's
# own chunk size) and the callback (~_FRAMES_PER_BUFFER/output_rate cadence,
# paced by the audio device's own clock) drain at different, independently-
# scheduled rates - even when production is genuinely keeping up with real
# time on average, the buffer can quite normally graze empty for a single
# callback purely from that timing mismatch, with no real underrun at all.
# Re-priming (a full _PRIME_SECONDS of enforced silence) on every single one
# of those normal, harmless grazes was turning brief, barely-there gaps into
# a real ~200ms silence every time - actively *causing* the periodic
# choppiness this was supposed to prevent. Requiring several in a row before
# treating it as a real stall filters that out while still catching genuine
# sustained underruns.
_REPRIME_STREAK = 4

# Proportional gain for the drift corrector (see point 4 in the module
# docstring): fraction of the current buffer-vs-target error (in samples)
# corrected per write() call. Deliberately small - this trades speed of
# convergence for making each individual correction inaudible; the ring
# buffer's cushion (_PRIME_SECONDS) is what actually absorbs the error while
# this gradually works it back down to zero.
#
# Proportional-only isn't actually enough against a *persistent* clock-rate
# mismatch though: textbook control theory (verified numerically before
# this shipped) says a P-only controller facing a constant-rate disturbance
# settles at a permanent offset below/above target instead of ever reaching
# it - simulating a real, sustained clock mismatch showed the buffer
# stabilizing at under half its target level rather than actually recovering
# it. _DRIFT_INTEGRAL_GAIN adds the integral term that's specifically for
# eliminating exactly that steady-state error - much smaller and clamped
# (_DRIFT_INTEGRAL_CLAMP) since it accumulates *across* calls instead of
# reacting only to the current one, and an unclamped integral term can "wind
# up" during a long startup transient or silence and overshoot once real
# audio resumes.
_DRIFT_GAIN = 0.002
_DRIFT_INTEGRAL_GAIN = 0.00002
_DRIFT_INTEGRAL_CLAMP = 200.0
# Never adjust a single chunk's length by more than this fraction of itself
# in one call, however large the error - keeps every individual correction
# small even right after a startup/re-prime when the error is at its peak.
_MAX_DRIFT_ADJUST_FRACTION = 0.25


def _to_stereo(audio: np.ndarray) -> np.ndarray:
    """Normalizes demodulator output to an (n, 2) float32 array: mono (n,)
    is duplicated to both channels, stereo (n, 2) passes through unchanged."""
    if audio.ndim == 1:
        return np.repeat(audio[:, None], 2, axis=1)
    return audio


def _retime(audio: np.ndarray, target_len: int) -> np.ndarray:
    """Linearly resamples ``audio`` (already at the device's output rate)
    from its current length to ``target_len`` - used only for the drift
    corrector's tiny (a handful of samples out of a chunk of hundreds)
    nudges, so the interpolation error is inaudible. Not used for the real
    input-rate -> output-rate conversion - that's resample_poly, in
    AudioOutput._resample_streaming, chosen there specifically for quality."""
    m = audio.shape[0]
    if target_len == m or m < 2 or target_len < 2:
        return audio
    src_idx = np.linspace(0, m - 1, target_len)
    idx0 = np.clip(src_idx.astype(np.int64), 0, m - 2)
    frac = (src_idx - idx0)[:, None]
    return (audio[idx0] * (1 - frac) + audio[idx0 + 1] * frac).astype(np.float32)


class AudioOutput:
    def __init__(self, sample_rate: int, device_index: int = -1):
        self.sample_rate = sample_rate
        self.output_rate = sample_rate
        self._pa = None
        self._stream = None
        self._resample_up = 1
        self._resample_down = 1
        # Real (not zero) samples kept purely as left-context for the next
        # resample_poly call, and a same-sized margin of not-yet-resampled
        # samples held back at the tail of _pending to serve as the *next*
        # call's real right-context - see point 2 in the module docstring.
        self._left_history = np.zeros((0, 2), dtype=np.float32)
        self._margin = 0
        self._pending = np.zeros((0, 2), dtype=np.float32)
        # Ring buffer PortAudio's callback (its own thread) drains from -
        # see point 3 in the module docstring.
        self._buffer_lock = threading.Lock()
        self._buffer = np.zeros((0, 2), dtype=np.float32)
        # Fractional carry for the drift corrector (see point 4 and
        # _correct_drift) - the per-call correction is usually well under
        # one whole sample, this is what accumulates it until it's actually
        # due.
        self._drift_debt = 0.0
        self._drift_integral = 0.0  # slow-moving bias - see _DRIFT_INTEGRAL_GAIN
        # True whenever the callback should hold off on real playback until
        # the buffer has built back up to _prime_len - see point 3b above.
        # Starts True so the very first playback of a session also gets the
        # cushion, not just recoveries from a later underrun.
        self._priming = True
        self._prime_len = 0
        self._empty_streak = 0
        # See _boost_priority_once() - attempted lazily, once, from inside
        # the callback itself, since PortAudio (not this class) owns and
        # starts that thread.
        self._priority_boosted = False
        # Pre-allocated fixed-size buffer, not a growing list - see the
        # _DEBUG constant's docstring for why that matters here specifically.
        self._capture: np.ndarray | None = None
        self._capture_pos = 0
        if not HAVE_PYAUDIO and not HAVE_SOUNDDEVICE:
            return
        try:
            if HAVE_PYAUDIO:
                self._pa = pyaudio.PyAudio()
                output_rate = self._pick_output_rate(device_index)
                kwargs = dict(
                    format=pyaudio.paFloat32, channels=2, rate=output_rate, output=True,
                    frames_per_buffer=_FRAMES_PER_BUFFER, stream_callback=self._pa_callback,
                )
                if device_index is not None and device_index >= 0:
                    kwargs["output_device_index"] = device_index
                self._stream = self._pa.open(**kwargs)
            else:
                output_rate = self._pick_output_rate(device_index)
                self._stream = sounddevice.OutputStream(
                    samplerate=output_rate, channels=2, dtype="float32",
                    blocksize=_FRAMES_PER_BUFFER, callback=self._sd_callback,
                    device=device_index if device_index is not None and device_index >= 0 else None,
                )
                self._stream.start()
            self.output_rate = output_rate
            if _DEBUG:
                self._capture = np.zeros((int(output_rate * _CAPTURE_MAX_SECONDS), 2), dtype=np.float32)
            self._max_buffer_len = int(output_rate * _MAX_BUFFER_SECONDS)
            self._prime_len = int(output_rate * _PRIME_SECONDS)
            if output_rate != sample_rate:
                g = int(np.gcd(int(output_rate), int(sample_rate)))
                self._resample_up = output_rate // g
                self._resample_down = int(sample_rate) // g
                self._margin = min(int(sample_rate * 0.01), 256)
                self._left_history = np.zeros((self._margin, 2), dtype=np.float32)
        except Exception:
            self._stream = None

    def _pick_output_rate(self, device_index: int) -> int:
        try:
            if self._pa is None:
                if device_index is not None and device_index >= 0:
                    info = sounddevice.query_devices(device_index)
                else:
                    info = sounddevice.query_devices(kind="output")
                rate = int(round(info.get("default_samplerate", 0)))
                return rate if rate > 0 else 48000
            if device_index is not None and device_index >= 0:
                info = self._pa.get_device_info_by_index(device_index)
            else:
                info = self._pa.get_default_output_device_info()
            rate = int(round(info.get("defaultSampleRate", 0)))
            return rate if rate > 0 else 48000
        except Exception:
            return 48000

    @property
    def available(self) -> bool:
        return self._stream is not None

    @property
    def buffered_seconds(self) -> float:
        """How much audio is currently queued, waiting to be played -
        diagnostic only (see SMARTSCAN_DEBUG_LIVE_AUDIO in scanner_engine.py).
        Hovering near _PRIME_SECONDS is healthy. A value that keeps *growing*
        tick after tick means production is outrunning real-time playback -
        the opposite of an underrun, but just as audible: rising delay, then
        a jump once _MAX_BUFFER_SECONDS forces a skip."""
        if self.output_rate <= 0:
            return 0.0
        with self._buffer_lock:
            n = self._buffer.shape[0]
        return n / self.output_rate

    def _pa_callback(self, in_data, frame_count, time_info, status):
        """Runs on PortAudio's own thread, on demand - never on the scanner
        thread, and never blocked by it. Silently primes (see _PRIME_SECONDS)
        before playing anything on session start and after a *sustained*
        underrun (_REPRIME_STREAK consecutive empty callbacks - see there for
        why one single empty callback deliberately does NOT re-arm this), and
        pads with silence if it's caught up short even so (only possible if
        the scanner is genuinely producing audio slower than real time for
        longer than the cushion covers) rather than glitching."""
        return (np.ascontiguousarray(self._next_frames(frame_count)).tobytes(), pyaudio.paContinue)

    def _sd_callback(self, outdata, frames, time_info, status) -> None:
        """sounddevice's equivalent of _pa_callback - same PortAudio thread,
        fills the given buffer in place instead of returning bytes."""
        outdata[:] = self._next_frames(frames)

    def _boost_priority_once(self) -> None:
        """Best-effort real-time (SCHED_FIFO) priority request for whichever
        thread PortAudio is actually calling this callback on - the same
        fix, and the same reasoning, as AcquisitionThread._boost_priority()
        in sdr_backend.py (see gotcha 11): this callback is Python code, so
        it needs the GIL to run at all, same as every other thread in this
        process - if it's left at plain SCHED_OTHER while the SDR
        acquisition thread runs SCHED_FIFO nearby, the OS scheduler can
        (and, per gotcha 11's own measurements on this exact machine, does)
        keep preferring a runnable SCHED_FIFO thread over a normal-priority
        one with no fairness guarantee, so this callback can sit waiting for
        its turn at the GIL even though *audibly* it has an even harder
        real-time deadline than a retune does: a single missed callback is
        an instant, audible glitch, not just a slower sweep. Boosting this
        thread to the same priority class removes that specific class of
        starvation. Only attempted once (PortAudio always calls back on the
        same OS thread for the life of one stream) - can't be done in
        __init__ like AcquisitionThread does, since PortAudio (not this
        class) creates and owns that thread; the first callback invocation
        is the earliest point this code ever runs on it. Best-effort: a
        missing CAP_SYS_NICE/rtprio limit silently leaves it at normal
        priority, exactly like AcquisitionThread's own fallback."""
        self._priority_boosted = True
        if not hasattr(os, "sched_setscheduler"):
            return
        try:
            tid = threading.get_native_id()
            os.sched_setscheduler(tid, os.SCHED_FIFO, os.sched_param(10))
        except (PermissionError, OSError):
            pass

    def _next_frames(self, frame_count: int) -> np.ndarray:
        """The body shared by both PortAudio wrappers' callbacks - see
        _pa_callback's docstring."""
        if not self._priority_boosted:
            self._boost_priority_once()
        with self._buffer_lock:
            avail = self._buffer.shape[0]
            if avail == 0:
                self._empty_streak += 1
                if not self._priming and self._empty_streak >= _REPRIME_STREAK:
                    self._priming = True  # sustained dry spell - rebuild the cushion before resuming
                    if _DEBUG:
                        _dbg(f"re-priming after {self._empty_streak} consecutive empty callbacks")
            else:
                self._empty_streak = 0
            if self._priming:
                if avail < self._prime_len:
                    out = np.zeros((frame_count, 2), dtype=np.float32)
                    self._capture_write(out)
                    return out
                self._priming = False
            if avail >= frame_count:
                out = self._buffer[:frame_count]
                self._buffer = self._buffer[frame_count:]
            else:
                out = np.concatenate([self._buffer, np.zeros((frame_count - avail, 2), dtype=np.float32)])
                self._buffer = np.zeros((0, 2), dtype=np.float32)
            self._capture_write(out)
        return out

    def _capture_write(self, out: np.ndarray) -> None:
        """O(1)-ish slice-assign into the pre-allocated capture buffer - see
        the _DEBUG constant's docstring for why this can't be a growing
        list. Silently stops (not wraps) once _CAPTURE_MAX_SECONDS is used
        up, rather than overwriting the start of a session."""
        if self._capture is None:
            return
        n = out.shape[0]
        end = min(self._capture_pos + n, self._capture.shape[0])
        take = end - self._capture_pos
        if take > 0:
            self._capture[self._capture_pos:end] = out[:take]
            self._capture_pos = end

    def write(self, audio: np.ndarray) -> None:
        """Resamples (if needed) and hands audio to the ring buffer for
        PortAudio's callback to pick up - never blocks, so it's always safe
        to call from the time-sensitive scanner thread. Accepts mono (n,) or
        stereo (n, 2) - see _to_stereo()."""
        if self._stream is None or audio.size == 0:
            return
        try:
            audio = _to_stereo(audio.astype(np.float32))
            if self._resample_up != self._resample_down:
                audio = self._resample_streaming(audio)
                if audio.shape[0] == 0:
                    return
            audio = self._correct_drift(audio)
            with self._buffer_lock:
                self._buffer = np.concatenate([self._buffer, audio])
                if self._buffer.shape[0] > self._max_buffer_len:
                    self._buffer = self._buffer[-self._max_buffer_len:]
        except Exception:
            pass

    def _correct_drift(self, audio: np.ndarray) -> np.ndarray:
        """Nudges this chunk's sample count by a tiny, gradually-converging
        amount to correct the slow buffer-level drift from SDR/sound-card
        clock mismatch - see point 4 in the module docstring."""
        if audio.shape[0] < 8 or self._prime_len <= 0:
            return audio
        with self._buffer_lock:
            current = self._buffer.shape[0]
        error = self._prime_len - current  # + = buffer running low, needs MORE samples
        self._drift_integral += error * _DRIFT_INTEGRAL_GAIN
        self._drift_integral = max(-_DRIFT_INTEGRAL_CLAMP, min(_DRIFT_INTEGRAL_CLAMP, self._drift_integral))
        self._drift_debt += error * _DRIFT_GAIN + self._drift_integral
        n_correct = int(self._drift_debt)
        if n_correct == 0:
            return audio
        self._drift_debt -= n_correct
        limit = max(1, int(audio.shape[0] * _MAX_DRIFT_ADJUST_FRACTION))
        n_correct = max(-limit, min(limit, n_correct))
        return _retime(audio, audio.shape[0] + n_correct)

    def _resample_streaming(self, audio: np.ndarray) -> np.ndarray:
        """Resamples ``audio`` (n, 2) to the device's native rate without a
        click or artifact at the chunk boundary - see point 2 in the module
        docstring. Adds a small, constant ~margin-worth of latency (a few
        ms), which is irrelevant next to the scanner's own tuning/linger
        delays."""
        self._pending = np.concatenate([self._pending, audio])
        flush_len = self._pending.shape[0] - self._margin
        if flush_len <= 0:
            return np.zeros((0, 2), dtype=np.float32)  # not enough real right-context yet

        # combined = [real left-context][samples to actually emit][real
        # right-context, i.e. the margin] - every emitted sample's filter
        # window is backed by genuine audio on both sides.
        combined = np.concatenate([self._left_history, self._pending])
        resampled = resample_poly(combined, self._resample_up, self._resample_down, axis=0)

        trim_left = round(self._left_history.shape[0] * self._resample_up / self._resample_down)
        n_out = round(flush_len * self._resample_up / self._resample_down)
        out = resampled[trim_left: trim_left + n_out].astype(np.float32)

        if self._margin:
            self._left_history = self._pending[flush_len - self._margin: flush_len]
        self._pending = self._pending[flush_len:]
        return out

    def close(self) -> None:
        if self._stream is not None:
            try:
                if self._pa is not None:
                    self._stream.stop_stream()
                else:
                    self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
        self._dump_capture()

    def _dump_capture(self) -> None:
        if self._capture is None or self._capture_pos == 0:
            return
        try:
            import wave
            audio = self._capture[:self._capture_pos]
            pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
            with wave.open(str(_CAPTURE_PATH), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(self.output_rate)
                wf.writeframes(pcm16.tobytes())
            _dbg(f"wrote {audio.shape[0] / self.output_rate:.1f}s of exactly what was sent to "
                 f"PortAudio (silence included) to {_CAPTURE_PATH}")
        except Exception as exc:
            _dbg(f"failed to write live-audio capture: {exc}")
        finally:
            self._capture = None
            self._capture_pos = 0

    @staticmethod
    def list_output_devices() -> list[tuple[int, str]]:
        if not HAVE_PYAUDIO:
            if not HAVE_SOUNDDEVICE:
                return []
            try:
                return [
                    (i, info.get("name", f"device {i}"))
                    for i, info in enumerate(sounddevice.query_devices())
                    if info.get("max_output_channels", 0) > 0
                ]
            except Exception:
                return []
        devices = []
        pa = pyaudio.PyAudio()
        try:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if info.get("maxOutputChannels", 0) > 0:
                    devices.append((i, info.get("name", f"device {i}")))
        finally:
            pa.terminate()
        return devices
