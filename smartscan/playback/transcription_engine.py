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
import os
import threading
import time
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
# ``WhisperModel(..., device="cuda")`` może przejść poprawnie, a wyłożyć się
# dopiero na pierwszej inferencji, ponieważ ctranslate2 część alokacji robi
# leniwie. Nie przechodzimy jednak od razu na CPU: na kartach z ograniczonym
# VRAM (np. 8 GB) ``int8_float16`` zwykle pozwala dalej używać GPU z małą
# stratą jakości. CPU jest dopiero ostatnim fallbackiem - ale nie trwałym:
# patrz _force_cpu_since/_GPU_RETRY_COOLDOWN_S, bo "trwały do końca sesji"
# okazało się w praktyce oznaczać "prawie każda sesja", nie odosobniony
# przypadek (dziennik z ostatnich 4 dni: 7 na 7 sesji skończyło na CPU,
# zawsze na "CUDA failed with error out of memory") - GPU ma realnie
# 6+ GB wolne w spoczynku, więc to bardziej chwilowy skok/fragmentacja
# VRAM (np. po jednym wyjątkowo długim nagraniu) niż trwały brak zasobu,
# i zwykle miałoby szansę wrócić do działania, gdyby dać mu tę szansę.
_force_cpu = False
_force_cpu_since: Optional[float] = None
_gpu_compute_type: Optional[str] = None
# Po tylu sekundach na CPU spróbuj GPU od nowa (samoograniczające się: jeśli
# znowu się wyłoży, _force_cpu i _force_cpu_since po prostu ustawiają się na
# nowo, przesuwając kolejną próbę o tyle samo w przyszłość - zero ryzyka
# młócenia GPU co plik, jak przy oryginalnym, jeszcze bardziej upartym
# podejściu, które to zastąpiło).
_GPU_RETRY_COOLDOWN_S = 1800.0
# Transkrypcja na CPU (kiedy do niej dojdzie) domyślnie próbuje zająć
# WSZYSTKIE rdzenie - a to właśnie one są potrzebne w czasie rzeczywistym
# wątkowi skanera/audio. Zaobserwowane naprawdę (patrz sesja, która to
# dodała): zajęty procesor podnosi realny poziom szumu w nasłuchu (opóźnione
# readStream() -> sklejane nieciągłe próbki -> szerokopasmowy artefakt w
# FFT), co przy niskim trigger_level potrafi zalać wykrywanie fałszywkami -
# a każda z nich trafia do tej samej kolejki transkrypcji, jeszcze bardziej
# obciążając CPU. Zostawiamy realny margines rdzeni wolny zamiast pozwalać
# Whisperowi zająć wszystko, żeby nie zamykać tego błędnego koła samemu.
_CPU_RESERVED_CORES = 4


# Domyślny punkt startowy dla "auto" na GPU. Był kiedyś "float16", ale
# realne dzienniki (journalctl z tej karty, 8GB) pokazały, że to reliably
# za ciasne: oba modele (large-v3 + medium ATC) w float16 to ~4.5GB samych
# wag, i mimo że działa to przez pewien czas (zaobserwowane: np. 37 plików
# bez problemu), prędzej czy później - dłuższe nagranie albo zwykła
# fragmentacja alokatora CUDA po wielu wywołaniach z rzędu - i tak wyłoży
# się na "CUDA failed with error out of memory", po czym i tak lądowaliśmy
# w tym samym miejscu (int8_float16) tyle że po stracie czasu na próbę i
# błąd. Zaczynanie od razu tutaj nie gwarantuje zera awarii (wystarczająco
# długie nagranie może przeciążyć każdy wariant), ale realnie zostawia dużo
# więcej marginesu, więc powinno je uczynić rzadkimi zamiast rutynowymi.
_DEFAULT_AUTO_GPU_COMPUTE_TYPE = "int8_float16"


def _gpu_compute_type_for(config: TranscriptionConfig) -> str:
    """Zwraca aktualny wariant GPU dla tej sesji.

    ``int8_float16`` trzyma wagi modelu jako int8, ale nadal wykonuje
    obliczenia na GPU w float16. Jest więc właściwym planem B dla ``auto``,
    zamiast porzucania GPU po pojedynczym błędzie alokacji.
    """
    if _gpu_compute_type is not None:
        return _gpu_compute_type
    return _DEFAULT_AUTO_GPU_COMPUTE_TYPE if config.compute_type == "auto" else config.compute_type


def _is_cuda_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(token in message for token in ("cuda", "cudnn", "cublas"))


def _set_force_cpu(value: bool) -> None:
    """Ustawia _force_cpu razem z _force_cpu_since (do liczenia cooldownu
    przed kolejną próbą GPU - patrz _maybe_retry_gpu_locked)."""
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
                "Model transkrypcji %s załadowany na GPU (%s)",
                model_id, _gpu_compute_type_for(config),
            )
            return model
        except Exception as exc:  # noqa: BLE001 - dowolny powód (brak GPU, brak VRAM, brak libcublas...) -> CPU
            # Model angielski jest ładowany po ogólnym; przy małej ilości
            # VRAM jego załadowanie może być pierwszym miejscem, w którym
            # ujawnia się brak pamięci. Zanim wybierzemy CPU, spróbujmy
            # lżejszych wag na tej samej karcie.
            if config.compute_type == "auto" and _gpu_compute_type_for(config) == "float16":
                _gpu_compute_type = "int8_float16"
                logger.warning(
                    "Nie udało się załadować %s na GPU w float16 (%s); "
                    "ponawiam na GPU w int8_float16.", model_id, exc,
                )
                return _load_model(model_id, config)
            if device == "cuda":
                raise
            logger.warning(
                "Nie udało się załadować %s na GPU także w %s (%s), "
                "przechodzę na CPU (spróbuję znów GPU za ~%.0f min).",
                model_id, _gpu_compute_type_for(config), exc, _GPU_RETRY_COOLDOWN_S / 60,
            )
            _set_force_cpu(True)
    ct = config.compute_type if config.compute_type != "auto" else "int8"
    cpu_threads = _cpu_thread_count()
    model = WhisperModel(model_id, device="cpu", compute_type=ct, cpu_threads=cpu_threads)
    logger.info("Model transkrypcji %s załadowany na CPU (cpu_threads=%d)", model_id, cpu_threads)
    return model


def _maybe_retry_gpu_locked() -> None:
    """Wołane z _get_models() pod _lock: jeśli jesteśmy na CPU wystarczająco
    długo, daje GPU kolejną szansę zamiast trzymać się CPU do końca sesji -
    patrz _force_cpu_since dlaczego "do końca sesji" był w praktyce zbyt
    upartym założeniem. Samoograniczające się: nieudana próba i tak wraca
    na CPU przez _load_model(), tylko z odświeżonym _force_cpu_since, więc
    to nie staje się próbą co plik."""
    global _generic_model, _english_atc_model, _active_config
    if not _force_cpu or _force_cpu_since is None:
        return
    if time.monotonic() - _force_cpu_since < _GPU_RETRY_COOLDOWN_S:
        return
    logger.info("Transkrypcja: minął cooldown - próbuję ponownie GPU.")
    _set_force_cpu(False)
    _generic_model = None
    _english_atc_model = None
    _active_config = None


def _get_models(config: TranscriptionConfig):
    """Leniwie ładuje oba modele (dzielone między kolejnymi wywołaniami z wątku roboczego)."""
    global _generic_model, _english_atc_model, _active_config
    with _lock:
        _maybe_retry_gpu_locked()
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
    rec: Recording, config: TranscriptionConfig, _recovery_step: int = 0,
    forced_language: Optional[str] = None,
) -> TranscriptionResult:
    """Transkrybuje pojedyncze nagranie. Nie rzuca wyjątków na zewnątrz - błędy
    (uszkodzony plik, brak modelu, wyczerpana pamięć GPU...) są łapane i
    zwracane jako status "error", żeby jedno złe nagranie nie zatrzymało całej
    kolejki w tle.

    ``forced_language`` (kod ISO, np. "pl"/"en") pomija całkowicie
    detect_language()/_resolve_language() - używane wyłącznie z ręcznego
    "Transkrybuj ponownie" z wybranym językiem w Odtwarzaczu (na słabym
    audio detekcja bywa zawodna, patrz _resolve_language), nigdy z
    automatycznej kolejki w tle (TranscriptionWorker jej nie ustawia), więc
    to nie zmienia zachowania auto-transkrypcji w żaden sposób."""
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
            # Najpierw zresetuj oba modele i spróbuj lżejszych wag GPU.
            # Reset jest konieczny: model float16 mógł zająć VRAM zanim
            # błąd wyszedł przy właściwej inferencji.
            if config.compute_type == "auto" and _gpu_compute_type_for(config) == "float16" and _recovery_step == 0:
                logger.warning(
                    "Transkrypcja: GPU nie wykonało inferencji w float16 (%s); "
                    "ponawiam na GPU w int8_float16.", exc,
                )
                with _lock:
                    _gpu_compute_type = "int8_float16"
                    _generic_model = None
                    _english_atc_model = None
                    _active_config = None
                return transcribe_recording(rec, config, _recovery_step=1, forced_language=forced_language)

            # Dopiero druga awaria GPU (albo awaria wariantu wymuszonego
            # przez użytkownika) daje CPU. Ten retry jest ograniczony, więc
            # nie zalewamy logu kolejnymi próbami dla każdego pliku. Nie na
            # zawsze - patrz _GPU_RETRY_COOLDOWN_S: to była poprzednia,
            # zbyt uparta wersja tego komentarza.
            if _recovery_step < 2:
                logger.warning(
                    "Transkrypcja: GPU nie dziala także po ponowieniu (%s) - "
                    "przechodzę na CPU (spróbuję znów GPU za ~%.0f min).",
                    exc, _GPU_RETRY_COOLDOWN_S / 60,
                )
                with _lock:
                    _set_force_cpu(True)
                    _generic_model = None
                    _english_atc_model = None
                    _active_config = None
                return transcribe_recording(rec, config, _recovery_step=2, forced_language=forced_language)
        logger.exception("Transkrypcja nie powiodła się dla %s", rec.path)
        return TranscriptionResult(
            text="", language="", language_probability=0.0,
            status="error", engine="", transcribed_at=now,
        )
