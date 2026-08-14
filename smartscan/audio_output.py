"""Live audio monitoring output (speaker), backed directly by PyAudio/PortAudio.

Degrades gracefully to a no-op if PyAudio isn't installed, so the rest of
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
   demodulates, optionally runs spectral-gate noise reduction, and does a
   full FFT for the live spectrum view (see ScannerEngine._emit_spectrum),
   all *before* handing audio to this class. If any of that work is
   momentarily slower than the chunk's real-time duration (GIL contention,
   a big FFT at a high capture rate, OS scheduling jitter, ...), calling
   PortAudio's blocking stream.write() straight from that same thread means
   PortAudio's own buffer can starve and underrun right along with it -
   an audible crackle/glitch that has nothing to do with the *content* of
   the audio (a recording is never subject to a real-time deadline, so it
   never shows this). Fix: don't call the blocking write() from the busy
   scanner thread at all. Push resampled audio into a small ring buffer
   instead (cheap, non-blocking) and let PortAudio pull from it via its own
   callback, on its own dedicated thread, whenever *it* needs more - fully
   decoupled from whatever the scanner thread happens to be busy doing.

The output stream is always opened with 2 channels: mono audio (AM/NFM/WFM
without stereo) is simply duplicated to L=R, and true stereo audio (WFM with
"Stereo" enabled, see dsp.py) is passed straight through - so the stream
never needs reopening when a channel's mono/stereo-ness changes mid-session.
Everything below operates on (n, 2) float32 arrays uniformly.
"""
from __future__ import annotations

import threading

import numpy as np
from scipy.signal import resample_poly

try:
    import pyaudio
    HAVE_PYAUDIO = True
except ImportError:
    HAVE_PYAUDIO = False

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


def _to_stereo(audio: np.ndarray) -> np.ndarray:
    """Normalizes demodulator output to an (n, 2) float32 array: mono (n,)
    is duplicated to both channels, stereo (n, 2) passes through unchanged."""
    if audio.ndim == 1:
        return np.repeat(audio[:, None], 2, axis=1)
    return audio


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
        if not HAVE_PYAUDIO:
            return
        try:
            self._pa = pyaudio.PyAudio()
            output_rate = self._pick_output_rate(device_index)
            kwargs = dict(
                format=pyaudio.paFloat32, channels=2, rate=output_rate, output=True,
                frames_per_buffer=_FRAMES_PER_BUFFER, stream_callback=self._pa_callback,
            )
            if device_index is not None and device_index >= 0:
                kwargs["output_device_index"] = device_index
            self._stream = self._pa.open(**kwargs)
            self.output_rate = output_rate
            self._max_buffer_len = int(output_rate * _MAX_BUFFER_SECONDS)
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

    def _pa_callback(self, in_data, frame_count, time_info, status):
        """Runs on PortAudio's own thread, on demand - never on the scanner
        thread, and never blocked by it. Pads with silence on underrun
        (only possible if the scanner is genuinely producing audio slower
        than real time, e.g. a CPU-starved machine, not from per-chunk
        jitter) rather than glitching."""
        with self._buffer_lock:
            avail = self._buffer.shape[0]
            if avail >= frame_count:
                out = self._buffer[:frame_count]
                self._buffer = self._buffer[frame_count:]
            else:
                out = np.concatenate([self._buffer, np.zeros((frame_count - avail, 2), dtype=np.float32)])
                self._buffer = np.zeros((0, 2), dtype=np.float32)
        return (np.ascontiguousarray(out).tobytes(), pyaudio.paContinue)

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
            with self._buffer_lock:
                self._buffer = np.concatenate([self._buffer, audio])
                if self._buffer.shape[0] > self._max_buffer_len:
                    self._buffer = self._buffer[-self._max_buffer_len:]
        except Exception:
            pass

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
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass

    @staticmethod
    def list_output_devices() -> list[tuple[int, str]]:
        if not HAVE_PYAUDIO:
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
