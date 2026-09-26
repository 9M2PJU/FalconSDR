"""Persistent configuration for SmartScanATC.

Everything is dumped to ~/.config/SmartScanATC/config.json so the app never
needs to be reconfigured after a restart, per spec section 4.5.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "SmartScanATC"
CONFIG_PATH = CONFIG_DIR / "config.json"
CHANNELS_CSV = CONFIG_DIR / "channels.csv"
BLACKLIST_CSV = CONFIG_DIR / "blacklist.csv"
DEFAULT_RECORDINGS_DIR = str(Path.home() / "SmartScanATC" / "recordings")


@dataclass
class DeviceConfig:
    driver: str = "auto"          # "auto", "rtlsdr", "simulated", or a SoapySDR driver key
    device_args: str = ""         # extra SoapySDR device args, e.g. "serial=00000001"
    sample_rate_hz: float = 2_400_000.0  # capture bandwidth; RTL-SDR handles ~2.4 MSPS
    gain_mode: str = "auto"       # "auto" or "manual"
    gain_db: float = 30.0
    audio_output_device: int = -1  # -1 = system default (PyAudio index)


@dataclass
class VfoRangeConfig:
    start_hz: int = 118_000_000
    stop_hz: int = 136_000_000
    step_hz: int = 25_000
    label: str = ""       # display name in the band list (preset name or user-entered)
    enabled: bool = True  # unchecked bands stay in the list but are skipped while scanning
    modulation: str = "AM"  # Modulation.value used to demod this band's channels
    # Scan priority weight - 0 (default) means "scan in the usual round-robin
    # order, no special treatment", same as Channel.weight (see
    # scanner_engine._WeightedScheduler for how bands and channels compete).
    weight: float = 0.0
    # "Zaawansowane" per-band overrides (Scanner tab, _BandRow's advanced
    # button) - None means "use the global slider's value", same as every
    # other band without an override. Motivating case: civil aviation VHF
    # runs noisier than a fire-brigade band, so one global trigger level is
    # always a compromise between "too sensitive on the noisy band" and "too
    # deaf on the quiet one" - this lets each band carry its own. See
    # ScannerEngine._effective_* for how these get resolved per window.
    trigger_level_dbfs: float | None = None
    tuning_time_ms: int | None = None
    linger_time_ms: int | None = None
    gain_mode: str | None = None
    gain_db: float | None = None
    noise_reduction_enabled: bool | None = None
    noise_reduction_strength: float | None = None

    @property
    def has_advanced_overrides(self) -> bool:
        """True once at least one "zaawansowane" field above has actually
        been saved (see ScannerPanel's Save/Reset) - distinguishes a band
        that's merely *tagged* on a window (see ScanWindow.band) from one
        that's genuinely diverging from the global settings, e.g. for the
        spectrum view's trigger-line accent color."""
        return any((
            self.trigger_level_dbfs is not None, self.tuning_time_ms is not None,
            self.linger_time_ms is not None, self.gain_mode is not None,
            self.gain_db is not None, self.noise_reduction_enabled is not None,
            self.noise_reduction_strength is not None,
        ))


@dataclass
class TimingConfig:
    tuning_time_ms: int = 60
    scanning_dwell_ms: int = 45
    linger_time_ms: int = 1500


@dataclass
class RecordingConfig:
    enabled: bool = True
    mode: str = "split"          # "single" or "split"
    output_dir: str = DEFAULT_RECORDINGS_DIR
    insert_marker: bool = True


@dataclass
class LocationConfig:
    """Receiver location - used only to build the FlightRadar24/ADSBExchange
    playback links in Odtwarzacz (an approximate location is plenty precise
    for a ~9-zoom flight-tracker map, not exact GPS coordinates). None means
    "not set yet" - see PlaybackPanel._open_fr24_for/_open_adsbexchange_for,
    which redirect to Options with an explanatory message instead of opening
    a link built from nothing. Deliberately no baked-in default location:
    this used to default to the original developer's own home coordinates,
    which has no business being anyone else's default in a published app."""
    home_lat: float | None = None
    home_lon: float | None = None


@dataclass
class RecencyBoostConfig:
    """After a transmission ends *naturally* (linger expires, not a manual
    Skip) its window gets temporarily revisited more often than the normal
    weighted rotation would give it - a follow-up transmission on the same
    frequency often lands within the next several/dozens of seconds (typical
    ATC back-and-forth). Unlike just raising linger_time_ms, this never
    blocks the scanner from noticing the conversation move to a *different*
    frequency in the meantime - it keeps circling the rest of the schedule
    as normal, just with this one window's turn coming up more often for a
    while (see ScannerEngine._due_recency_boost_locked)."""
    enabled: bool = True
    duration_s: float = 90.0   # how long after the transmission ends to keep boosting
    interval_s: float = 8.0    # how often (real seconds) to sneak in an extra visit during the boost


@dataclass
class NoiseReductionConfig:
    enabled: bool = False
    strength: float = 0.6        # 0..1, blend between dry (0) and fully gated (1) spectrum


@dataclass
class TranscriptionConfig:
    enabled: bool = True         # automatyczna transkrypcja nowych nagrań w tle (zakładka Odtwarzacz)
    generic_model: str = "large-v3"  # wielojęzyczny model (m.in. polskie transmisje)
    english_atc_model: str = "jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper"
    device: str = "auto"         # "auto" | "cuda" | "cpu"
    compute_type: str = "auto"   # "auto" -> int8_float16 na GPU, int8 na CPU
    # Języki, na które obstawiamy wynik detekcji w niepewnych przypadkach (patrz
    # transcription_engine._resolve_language) - w tym nasłuchu realnie liczy się
    # tylko angielski i polski, więc szum radiowy nie myli detekcji egzotycznymi
    # językami.
    candidate_languages: list[str] = field(default_factory=lambda: ["en", "pl"])
    min_duration_sec: float = 0.6  # krótsze nagrania (np. sam trzask squelchu) pomijamy


@dataclass
class AppConfig:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    # Bands/ranges swept together in one scan session - presets, custom ranges
    # and single frequencies (start_hz == stop_hz) all live in this one list so
    # they can be freely added/removed/edited from the Scanner tab (see
    # military_presets.BAND_PRESETS for the built-in preset catalogue). The
    # scanner always watches these AND every enabled Memory List channel in
    # the same session - there is no separate "mode" to switch between.
    bands: list[VfoRangeConfig] = field(
        default_factory=lambda: [
            VfoRangeConfig(start_hz=118_000_000, stop_hz=136_975_000, step_hz=8_333, label="Lotnictwo cywilne")
        ]
    )
    timing: TimingConfig = field(default_factory=TimingConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    recency_boost: RecencyBoostConfig = field(default_factory=RecencyBoostConfig)
    noise_reduction: NoiseReductionConfig = field(default_factory=NoiseReductionConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    trigger_level_dbfs: float = -30.0
    # Extra cutoff for BOTH the live speaker and the recording, expressed as
    # a dB *offset from trigger_level_dbfs* (always <= 0), never as an
    # independent absolute value - see ScannerEngine._effective_squelch_dbfs()
    # and AGENTS.md gotcha 6 for why that distinction matters. Muting only
    # ever kicks in during LINGER (level already < trigger, slot still open
    # on the timeout grace period) to cut the loud FM-discriminator noise
    # burst right after a transmitter releases PTT (see dsp.py's demod() -
    # phase-derivative FM discrimination on pure noise produces loud
    # broadband output despite low real RF power once the actual carrier is
    # gone - the "FM threshold effect") from both live monitoring AND the
    # recorded file, so a recording ends where the real signal actually did
    # rather than trailing off into that noise. The gap between this and
    # trigger stays audible/recorded, since genuine content can briefly dip
    # below trigger during LINGER without being noise. Off by default -
    # purely opt-in, never changes anyone's existing listening/recording
    # behaviour unprompted.
    squelch_enabled: bool = False
    squelch_offset_db: float = -6.0
    default_blacklist_margin_hz: int = 5000
    memory_channel_bandwidth_hz: int = 12_500  # per-channel power window used to watch Memory List channels
    # Master on/off for the whole Memory List, independent of each channel's
    # own enabled flag - mirrors a band's own enabled checkbox. If off, no
    # Memory List channel is scanned even if individually enabled.
    memory_list_enabled: bool = True
    # Known Memory List folder names - includes empty folders created via
    # "Nowy folder" that have no channels filed under them yet, so they don't
    # disappear from the tree; folders that do have channels are re-derived
    # from Channel.folder on load regardless of what's in this list.
    channel_folders: list[str] = field(default_factory=list)
    # Decode FM broadcast stereo (pilot-tone L/R) for live monitoring only -
    # has no effect on AM/NFM. Recordings stay mono regardless.
    wfm_stereo_enabled: bool = False
    # UI language - a code with a matching smartscan/locale/<code>.po file,
    # "en" (the source language, built into every string, needs no file), or
    # "auto" (default for a fresh install - follow the OS's own language,
    # falling back to English if we have no translation for it). See
    # smartscan/i18n.py's resolve_system_language()/set_language(). Applying
    # a change needs a restart - see MainWindow._restart_app().
    language: str = "auto"

    @staticmethod
    def load() -> "AppConfig":
        if not CONFIG_PATH.exists():
            cfg = AppConfig()
            cfg.save()
            return cfg
        try:
            raw = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return AppConfig()
        if "bands" in raw:
            bands = [VfoRangeConfig(**b) for b in raw["bands"]]
        else:
            # Migrate pre-band-list configs (single "vfo_range" + "extra_vfo_ranges").
            legacy_primary = raw.get("vfo_range")
            bands = [VfoRangeConfig(**legacy_primary)] if legacy_primary else [VfoRangeConfig()]
            bands += [VfoRangeConfig(**r) for r in raw.get("extra_vfo_ranges", [])]
        # audio_format ("mp3") was removed - drop it if present in an older config.json
        recording_raw = dict(raw.get("recording", {}))
        recording_raw.pop("audio_format", None)
        return AppConfig(
            device=DeviceConfig(**raw.get("device", {})),
            bands=bands,
            timing=TimingConfig(**raw.get("timing", {})),
            recording=RecordingConfig(**recording_raw),
            recency_boost=RecencyBoostConfig(**raw.get("recency_boost", {})),
            noise_reduction=NoiseReductionConfig(**raw.get("noise_reduction", {})),
            transcription=TranscriptionConfig(**raw.get("transcription", {})),
            location=LocationConfig(**raw.get("location", {})),
            trigger_level_dbfs=raw.get("trigger_level_dbfs", -30.0),
            squelch_enabled=raw.get("squelch_enabled", False),
            squelch_offset_db=raw.get("squelch_offset_db", -6.0),
            default_blacklist_margin_hz=raw.get("default_blacklist_margin_hz", 5000),
            memory_channel_bandwidth_hz=raw.get("memory_channel_bandwidth_hz", 12_500),
            channel_folders=list(raw.get("channel_folders", [])),
            memory_list_enabled=raw.get("memory_list_enabled", True),
            wfm_stereo_enabled=raw.get("wfm_stereo_enabled", False),
            language=raw.get("language", "auto"),
        )

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
