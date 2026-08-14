"""Silnik transkrypcji nagrań pilot-ATC: wykrycie języka + Whisper (faster-whisper),
z osobnym modelem dostrojonym do angielskiej frazeologii lotniczej dla nagrań
angielskich i ogólnym wielojęzycznym modelem dla reszty (głównie polskich).

To nie jest transkrypcja "na żywo" - liczy się jakość, nie szybkość, więc
świadomie używamy największych praktycznych modeli i pełnego beam search
zamiast greedy/streaming.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from smartscan.config import TranscriptionConfig
from smartscan.playback.models import Recording, TranscriptionResult

logger = logging.getLogger(__name__)

# -- ładowanie bibliotek CUDA -------------------------------------------------
# ctranslate2 (silnik faster-whisper) od wersji 4.x nie dołącza już bibliotek
# cuBLAS/cuDNN do koła pip - trzeba je doinstalować osobno jako pakiety
# nvidia-cublas-cu12 / nvidia-cudnn-cu12 i "podać" mu je przed użyciem GPU.
# Ustawianie LD_LIBRARY_PATH z Pythona w trakcie działania procesu jest
# zawodne (zależy od tego, czy dynamic linker doczyta zmienną przed dlopen
# wykonanym już w trakcie działania programu, długo po starcie), więc zamiast
# tego jawnie ładujemy .so przez ctypes z RTLD_GLOBAL - gdy ctranslate2 zawoła
# dlopen("libcublas.so.12"), biblioteka jest już w procesie. Sprawdzone
# działanie na RTX 2070 / sterownik CUDA 13 / wheels CUDA 12.
_cuda_preloaded = False


def _preload_cuda_libs() -> None:
    global _cuda_preloaded
    if _cuda_preloaded:
        return
    _cuda_preloaded = True
    try:
        import nvidia  # z pakietów nvidia-cublas-cu12 / nvidia-cudnn-cu12, jeśli zainstalowane w venv
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
                logger.debug("Nie udało się załadować %s: %s", so, exc)


# Świadomie NIE używamy tu Whisperowego initial_prompt z przykładową
# frazeologią lotniczą. Wyglądało to na dobry pomysł (naprowadzenie słownictwa),
# ale na realnych nagraniach z tego projektu okazało się niebezpieczne: przy
# niepewnym/zaszumionym audio model potrafił dosłownie przepisać fragmenty
# promptu jako "transkrypcję" (odtwarzalne na 2026-07-26_05-41-12_131575.wav -
# wynikiem był niemal dokładny cytat z promptu), a nawet krótsza wersja
# (sama lista słów kluczowych) czasem pogarszała jakość na dobrych nagraniach
# zamiast pomagać. Fabrykowanie wiarygodnie brzmiącego, ale zmyślonego tekstu
# jest gorsze niż brak transkrypcji, więc zostajemy przy czystym dekodowaniu.

# Tylko te dwa języki realnie występują w tym nasłuchu - reszta to prawie zawsze
# błąd detekcji języka na zaszumionym sygnale VHF (patrz _resolve_language).

# Progi dobrane empirycznie na prawdziwych nagraniach z tego nasłuchu: same
# liczby (avg_logprob, no_speech_prob) słabo rozdzielają dobrą transkrypcję od
# halucynacji na szumie, dlatego status "low_confidence" traktujemy jako
# ostrzeżenie ("niepewne"), a nie powód do ukrycia tekstu.
_LOW_LANG_PROBABILITY = 0.4
_LOW_AVG_LOGPROB = -0.9
_HIGH_NO_SPEECH_PROB = 0.5
_HIGH_COMPRESSION_RATIO = 2.4

_lock = threading.Lock()
_generic_model = None
_english_atc_model = None
_active_config: Optional[TranscriptionConfig] = None
# Set once an actual inference call (not just model construction) fails with
# a CUDA error - see transcribe_recording(). WhisperModel(..., device="cuda")
# can succeed even when the GPU can't actually run it (ctranslate2 allocates
# lazily on first real use), so _load_model()'s own try/except doesn't catch
# this - it only catches load-time failures. Sticky for the rest of the
# process: a GPU that's out of memory or has an invalid device ordinal isn't
# going to fix itself mid-session, and retrying it per-file was hammering
# the GPU/CPU on every single transcription (visible as a wall of repeated
# "CUDA failed"/"invalid device ordinal" tracebacks, one set per file, over
# and over) badly enough to be a real resource-contention problem for
# anything else running at the same time.
_force_cpu = False


def _is_cuda_error(exc: BaseException) -> bool:
    return "cuda" in str(exc).lower()


def _load_model(model_id: str, config: TranscriptionConfig):
    from faster_whisper import WhisperModel

    device = "cpu" if _force_cpu else config.device
    compute_type = config.compute_type
    if device in ("auto", "cuda"):
        _preload_cuda_libs()
        try:
            model = WhisperModel(
                model_id, device="cuda",
                compute_type="float16" if compute_type == "auto" else compute_type,
            )
            logger.info("Model transkrypcji %s załadowany na GPU", model_id)
            return model
        except Exception as exc:  # noqa: BLE001 - dowolny powód (brak GPU, brak VRAM, brak libcublas...) -> CPU
            if device == "cuda":
                raise
            logger.warning("Nie udało się załadować %s na GPU (%s), używam CPU", model_id, exc)
    ct = compute_type if compute_type != "auto" else "int8"
    model = WhisperModel(model_id, device="cpu", compute_type=ct)
    logger.info("Model transkrypcji %s załadowany na CPU", model_id)
    return model


def _get_models(config: TranscriptionConfig):
    """Leniwie ładuje oba modele (dzielone między kolejnymi wywołaniami z wątku roboczego)."""
    global _generic_model, _english_atc_model, _active_config
    with _lock:
        if _active_config != config or _generic_model is None:
            _generic_model = _load_model(config.generic_model, config)
            _english_atc_model = _load_model(config.english_atc_model, config)
            _active_config = config
        return _generic_model, _english_atc_model


def _resolve_language(lang: str, prob: float, all_probs, candidates: list) -> tuple:
    """Detekcja języka Whisper na krótkich, zaszumionych klipach VHF bywa
    zawodna - na samym szumie potrafi z niską pewnością wskazać przypadkowy
    egzotyczny język (obserwowane na realnych nagraniach z tego projektu:
    norweski, fiński, rosyjski) zamiast prawdziwej treści angielskiej/polskiej.
    Jeśli zwycięski język nie jest jednym z oczekiwanych w tym nasłuchu, a
    któryś z oczekiwanych ma konkurencyjne prawdopodobieństwo, stawiamy na
    niego zamiast na egzotycznego "zwycięzcę"."""
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
    rec: Recording, config: TranscriptionConfig, _retried: bool = False,
) -> TranscriptionResult:
    """Transkrybuje pojedyncze nagranie. Nie rzuca wyjątków na zewnątrz - błędy
    (uszkodzony plik, brak modelu, wyczerpana pamięć GPU...) są łapane i
    zwracane jako status "error", żeby jedno złe nagranie nie zatrzymało całej
    kolejki w tle."""
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
        global _force_cpu, _generic_model, _english_atc_model, _active_config
        if not _retried and not _force_cpu and _is_cuda_error(exc):
            # The cached model(s) claimed to load fine on GPU but can't
            # actually run - force every model to reload on CPU from here
            # on, and give this one file a second, immediate chance on CPU
            # instead of parking it as "error" (which would otherwise just
            # get retried GPU-first again next batch - see _force_cpu above).
            logger.warning(
                "Transkrypcja: GPU nie dziala mimo poprawnego zaladowania modelu (%s) - "
                "trwale przechodze na CPU do konca sesji.", exc,
            )
            with _lock:
                _force_cpu = True
                _generic_model = None
                _english_atc_model = None
                _active_config = None
            return transcribe_recording(rec, config, _retried=True)
        logger.exception("Transkrypcja nie powiodła się dla %s", rec.path)
        return TranscriptionResult(
            text="", language="", language_probability=0.0,
            status="error", engine="", transcribed_at=now,
        )
