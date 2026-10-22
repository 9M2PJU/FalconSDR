"""Pilot-ATC recording transcription engine: language detection + Whisper
(faster-whisper), with a separate model fine-tuned for English aviation
phraseology for English recordings, and a general multilingual model for
the rest (mostly Polish).

This isn't "live" transcription - quality matters, not speed, so we
deliberately use the largest practical models and full beam search instead
of greedy/streaming decoding.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from falconsdr.config import TranscriptionConfig
from falconsdr.playback.models import Recording, TranscriptionResult

logger = logging.getLogger(__name__)

# -- loading CUDA libraries -------------------------------------------------
# ctranslate2 (faster-whisper's engine) no longer bundles cuBLAS/cuDNN in its
# pip wheel as of version 4.x - they need to be installed separately as the
# nvidia-cublas-cu12 / nvidia-cudnn-cu12 packages and "handed" to it before
# using the GPU. Setting LD_LIBRARY_PATH from Python while the process is
# already running is unreliable (depends on whether the dynamic linker reads
# the variable before a dlopen that happens later, long after startup), so
# instead we explicitly load the .so files via ctypes with RTLD_GLOBAL - by
# the time ctranslate2 calls dlopen("libcublas.so.12"), the library is
# already in the process. Verified working on an RTX 2070 / CUDA 13 driver /
# CUDA 12 wheels.
_cuda_preloaded = False


def _preload_cuda_libs() -> None:
    global _cuda_preloaded
    if _cuda_preloaded:
        return
    _cuda_preloaded = True
    try:
        import nvidia  # from the nvidia-cublas-cu12 / nvidia-cudnn-cu12 packages, if installed in the venv
    except ImportError:
        return
    base = Path(next(iter(nvidia.__path__)))
    for sub in ("cublas/lib", "cudnn/lib", "cuda_nvrtc/lib"):
        libdir = base / sub
        if not libdir.exists():
            continue
        for so in sorted(libdir.glob("lib*.so*")):
            try:
                ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
            except OSError as exc:
                logger.debug("Failed to load %s: %s", so, exc)


# Deliberately NOT using Whisper's initial_prompt with sample aviation
# phraseology here. It looked like a good idea (steering the vocabulary),
# but turned out dangerous on real recordings from this project: on
# uncertain/noisy audio the model could literally copy fragments of the
# prompt as the "transcription" (reproducible on
# 2026-07-26_05-41-12_131575.wav - the result was an almost exact quote of
# the prompt), and even a shorter version (just a keyword list) sometimes
# made good recordings worse instead of helping. Fabricating plausible-
# sounding but made-up text is worse than no transcription, so we stick with
# plain decoding.

# Only these two languages actually occur in this kind of listening - almost
# everything else is a language-detection error on a noisy VHF signal (see
# _resolve_language).

# Thresholds picked empirically on real recordings from this project: the
# raw numbers (avg_logprob, no_speech_prob) alone poorly separate a good
# transcription from hallucination on noise, so we treat "low_confidence" as
# a warning ("uncertain"), not a reason to hide the text.
_LOW_LANG_PROBABILITY = 0.4
_LOW_AVG_LOGPROB = -0.9
_HIGH_NO_SPEECH_PROB = 0.5
_HIGH_COMPRESSION_RATIO = 2.4

_lock = threading.Lock()
_generic_model = None
_english_atc_model = None
_active_config: Optional[TranscriptionConfig] = None
# ``WhisperModel(..., device="cuda")`` can succeed and only fail on the first
# inference, because ctranslate2 does some allocation lazily. We don't jump
# straight to CPU though: on cards with limited VRAM (e.g. 8 GB),
# ``int8_float16`` usually keeps the GPU usable with a small quality cost.
# CPU is only the last fallback - but not a permanent one: see
# _force_cpu_since/_GPU_RETRY_COOLDOWN_S, because "permanent for the rest of
# the session" turned out in practice to mean "almost every session", not an
# isolated case (a log from the last 4 days: 7 out of 7 sessions ended up on
# CPU, always with "CUDA failed with error out of memory") - the GPU
# genuinely has 6+ GB free at rest, so this is more of a momentary spike/
# fragmentation of VRAM (e.g. after one exceptionally long recording) than a
# durable lack of resources, and would usually have a chance to recover if
# given one.
_force_cpu = False
_force_cpu_since: Optional[float] = None
_gpu_compute_type: Optional[str] = None
# After this many seconds on CPU, try GPU again from scratch (self-limiting:
# if it fails again, _force_cpu and _force_cpu_since simply get set again,
# pushing the next attempt out by the same amount - zero risk of hammering
# the GPU on every file, unlike the original, even more stubborn approach
# this replaced). 1800s (30 min) was itself too stubborn in the other
# direction - real, reported case: one transient OOM (other GPU-using apps
# briefly spiking VRAM - Firefox/Steam/the desktop compositor, not this
# process) forced CPU transcription for the next half hour, visibly
# bogging down the whole machine the entire time. Measured directly: VRAM
# that momentarily squeezed out the models recovered within seconds, not
# minutes - a 1800s wait was paying for a problem that was already gone.
_GPU_RETRY_COOLDOWN_S = 120.0
# CPU transcription (once it gets there) tries to claim ALL cores by
# default - and those are exactly what the scanner/audio thread needs in
# real time. Actually observed (see the session that added this): a busy CPU
# raises the real noise floor in listening (delayed readStream() -> spliced
# non-contiguous samples -> a broadband artifact in the FFT), which at a low
# trigger_level can flood detection with false positives - and each one
# lands in this same transcription queue, loading the CPU even further. We
# leave a real margin of cores free instead of letting Whisper take
# everything, so we don't close that vicious circle ourselves.
_CPU_RESERVED_CORES = 4


# Default starting point for "auto" on GPU. Used to be "float16", but real
# logs (journalctl from this card, 8GB) showed that's reliably too tight:
# both models (large-v3 + medium ATC) in float16 is ~4.5GB of weights alone,
# and even though it works for a while (observed: e.g. 37 files with no
# problem), sooner or later - a longer recording, or plain CUDA allocator
# fragmentation after many calls in a row - it fails with "CUDA failed with
# error out of memory" anyway, after which we ended up in the same place
# (int8_float16) regardless, just after losing time to the attempt and
# error. Starting here directly doesn't guarantee zero failures (a long
# enough recording can overload any variant), but it genuinely leaves much
# more margin, so it should make them rare instead of routine.
_DEFAULT_AUTO_GPU_COMPUTE_TYPE = "int8_float16"


def _gpu_compute_type_for(config: TranscriptionConfig) -> str:
    """Returns the current GPU variant for this session.

    ``int8_float16`` keeps the model weights as int8, but still runs
    computation on the GPU in float16. So it's the right plan B for
    ``auto``, instead of giving up on the GPU after a single allocation error.
    """
    if _gpu_compute_type is not None:
        return _gpu_compute_type
    return _DEFAULT_AUTO_GPU_COMPUTE_TYPE if config.compute_type == "auto" else config.compute_type


def _is_cuda_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(token in message for token in ("cuda", "cudnn", "cublas"))


def _set_force_cpu(value: bool) -> None:
    """Sets _force_cpu together with _force_cpu_since (for counting down the
    cooldown before the next GPU attempt - see _maybe_retry_gpu_locked)."""
    global _force_cpu, _force_cpu_since
    _force_cpu = value
    _force_cpu_since = time.monotonic() if value else None


def _cpu_thread_count() -> int:
    total = os.cpu_count() or 4
    return max(1, total - _CPU_RESERVED_CORES)


def _load_model(model_id: str, config: TranscriptionConfig):
    global _gpu_compute_type
    from faster_whisper import WhisperModel

    device = "cpu" if _force_cpu else config.device
    if device in ("auto", "cuda"):
        _preload_cuda_libs()
        try:
            model = WhisperModel(
                model_id, device="cuda",
                compute_type=_gpu_compute_type_for(config),
            )
            logger.info(
                "Transcription model %s loaded on GPU (%s)",
                model_id, _gpu_compute_type_for(config),
            )
            return model
        except Exception as exc:  # noqa: BLE001 - any reason (no GPU, no VRAM, no libcublas...) -> CPU
            # The English model is loaded after the general one; with little
            # VRAM available, loading it can be the first place a memory
            # shortage shows up. Before falling back to CPU, try lighter
            # weights on the same card.
            if config.compute_type == "auto" and _gpu_compute_type_for(config) == "float16":
                _gpu_compute_type = "int8_float16"
                logger.warning(
                    "Failed to load %s on GPU in float16 (%s); "
                    "retrying on GPU in int8_float16.", model_id, exc,
                )
                return _load_model(model_id, config)
            if device == "cuda":
                raise
            logger.warning(
                "Failed to load %s on GPU even in %s (%s), "
                "falling back to CPU (will try GPU again in ~%.0f min).",
                model_id, _gpu_compute_type_for(config), exc, _GPU_RETRY_COOLDOWN_S / 60,
            )
            _set_force_cpu(True)
    ct = config.compute_type if config.compute_type != "auto" else "int8"
    cpu_threads = _cpu_thread_count()
    model = WhisperModel(model_id, device="cpu", compute_type=ct, cpu_threads=cpu_threads)
    logger.info("Transcription model %s loaded on CPU (cpu_threads=%d)", model_id, cpu_threads)
    return model


def _maybe_retry_gpu_locked() -> None:
    """Called from _get_models() under _lock: if we've been on CPU long
    enough, gives the GPU another chance instead of sticking with CPU for
    the rest of the session - see _force_cpu_since for why "for the rest of
    the session" turned out to be too stubborn an assumption in practice.
    Self-limiting: a failed attempt still falls back to CPU via
    _load_model(), just with a refreshed _force_cpu_since, so this never
    becomes a retry on every single file."""
    global _generic_model, _english_atc_model, _active_config
    if not _force_cpu or _force_cpu_since is None:
        return
    if time.monotonic() - _force_cpu_since < _GPU_RETRY_COOLDOWN_S:
        return
    logger.info("Transcription: cooldown elapsed - retrying GPU.")
    _set_force_cpu(False)
    _generic_model = None
    _english_atc_model = None
    _active_config = None


def _get_models(config: TranscriptionConfig):
    """Lazily loads both models (shared across subsequent calls from the worker thread)."""
    global _generic_model, _english_atc_model, _active_config
    with _lock:
        _maybe_retry_gpu_locked()
        if _active_config != config or _generic_model is None:
            _generic_model = _load_model(config.generic_model, config)
            _english_atc_model = _load_model(config.english_atc_model, config)
            _active_config = config
        return _generic_model, _english_atc_model


def _resolve_language(lang: str, prob: float, all_probs, candidates: list) -> tuple:
    """Whisper's language detection on short, noisy VHF clips can be
    unreliable - on pure noise it can point, with low confidence, at a
    random exotic language (observed on real recordings from this project:
    Norwegian, Finnish, Russian) instead of the real English/Polish content.
    If the winning language isn't one of the ones expected in this kind of
    listening, and one of the expected ones has a competitive probability,
    we go with it instead of the exotic "winner"."""
    if lang in candidates:
        return lang, prob
    probs = dict(all_probs)
    best_candidate = max(candidates, key=lambda l: probs.get(l, 0.0))
    best_candidate_prob = probs.get(best_candidate, 0.0)
    if best_candidate_prob >= prob * 0.6:
        return best_candidate, best_candidate_prob
    return lang, prob


def _classify_status(segments, lang_probability: float, duration_after_vad: float) -> str:
    if not segments or duration_after_vad < 0.3:
        return "no_speech"
    avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
    avg_no_speech = sum(s.no_speech_prob for s in segments) / len(segments)
    max_compression = max(s.compression_ratio for s in segments)
    if (
        lang_probability < _LOW_LANG_PROBABILITY
        or avg_logprob < _LOW_AVG_LOGPROB
        or avg_no_speech > _HIGH_NO_SPEECH_PROB
        or max_compression > _HIGH_COMPRESSION_RATIO
    ):
        return "low_confidence"
    return "ok"


def transcribe_recording(
    rec: Recording, config: TranscriptionConfig, _recovery_step: int = 0,
    forced_language: Optional[str] = None,
) -> TranscriptionResult:
    """Transcribes a single recording. Never raises an exception outward -
    errors (corrupt file, missing model, GPU out of memory...) are caught
    and returned as an "error" status, so one bad recording doesn't stop the
    whole background queue.

    ``forced_language`` (an ISO code, e.g. "pl"/"en") completely bypasses
    detect_language()/_resolve_language() - used only from a manual
    "Re-transcribe" with a chosen language on the Recordings page (detection can be
    unreliable on weak audio, see _resolve_language), never from the
    automatic background queue (TranscriptionWorker never sets it), so this
    never changes auto-transcription's behavior in any way."""
    now = datetime.now(timezone.utc).isoformat()

    if rec.duration_sec and rec.duration_sec < config.min_duration_sec:
        return TranscriptionResult(
            text="", language="", language_probability=0.0,
            status="skipped_short", engine="", transcribed_at=now,
        )

    try:
        from faster_whisper.audio import decode_audio

        generic_model, english_atc_model = _get_models(config)
        audio = decode_audio(str(rec.path))

        if forced_language:
            lang, prob = forced_language, 1.0
        else:
            raw_lang, raw_prob, all_probs = generic_model.detect_language(audio=audio, vad_filter=True)
            lang, prob = _resolve_language(raw_lang, raw_prob, all_probs, config.candidate_languages)

        if lang == "en":
            model, engine_id = english_atc_model, config.english_atc_model
        else:
            model, engine_id = generic_model, config.generic_model

        segments, info = model.transcribe(
            audio,
            language=lang,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        segments = list(segments)
        text = " ".join(s.text.strip() for s in segments).strip()
        status = _classify_status(segments, prob, info.duration_after_vad)

        return TranscriptionResult(
            text=text, language=lang, language_probability=prob,
            status=status, engine=engine_id, transcribed_at=now,
        )
    except Exception as exc:
        global _gpu_compute_type, _generic_model, _english_atc_model, _active_config
        if not _force_cpu and _is_cuda_error(exc):
            # First reset both models and try lighter GPU weights. The reset
            # is necessary: the float16 model may have claimed VRAM before
            # the error surfaced on the actual inference call.
            if config.compute_type == "auto" and _gpu_compute_type_for(config) == "float16" and _recovery_step == 0:
                logger.warning(
                    "Transcription: GPU inference failed in float16 (%s); "
                    "retrying on GPU in int8_float16.", exc,
                )
                with _lock:
                    _gpu_compute_type = "int8_float16"
                    _generic_model = None
                    _english_atc_model = None
                    _active_config = None
                return transcribe_recording(rec, config, _recovery_step=1, forced_language=forced_language)

            # Only a second GPU failure (or a failure of the variant the
            # user explicitly forced) falls back to CPU. This retry is
            # bounded, so we don't flood the log with repeated attempts for
            # every file. Not forever - see _GPU_RETRY_COOLDOWN_S: that was
            # this comment's previous, too-stubborn version.
            if _recovery_step < 2:
                logger.warning(
                    "Transcription: GPU doesn't work even after retrying (%s) - "
                    "falling back to CPU (will try GPU again in ~%.0f min).",
                    exc, _GPU_RETRY_COOLDOWN_S / 60,
                )
                with _lock:
                    _set_force_cpu(True)
                    _generic_model = None
                    _english_atc_model = None
                    _active_config = None
                return transcribe_recording(rec, config, _recovery_step=2, forced_language=forced_language)
        logger.exception("Transcription failed for %s", rec.path)
        return TranscriptionResult(
            text="", language="", language_probability=0.0,
            status="error", engine="", transcribed_at=now,
        )
