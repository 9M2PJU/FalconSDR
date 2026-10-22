"""Persistent configuration for FalconSDR.

Everything is dumped to ~/.config/FalconSDR/config.json so the app never
needs to be reconfigured after a restart, per spec section 4.5.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "FalconSDR"
CONFIG_PATH = CONFIG_DIR / "config.json"
CHANNELS_CSV = CONFIG_DIR / "channels.csv"
BLACKLIST_CSV = CONFIG_DIR / "blacklist.csv"
DEFAULT_RECORDINGS_DIR = str(Path.home() / "FalconSDR" / "recordings")


def _migrate_from_smartscanatc() -> None:
    """The app was called SmartScanATC until 2026-10: carry its settings
    over once (copy - the old folder stays as a backup), keeping the old
    default recordings folder so the library doesn't come up empty."""
    old = Path.home() / ".config" / "SmartScanATC"
    if CONFIG_DIR.exists() or not old.is_dir():
        return
    import shutil
    shutil.copytree(old, CONFIG_DIR)
    path = CONFIG_DIR / "config.json"
    try:
        raw = json.loads(path.read_text())
        raw.setdefault("recording", {}).setdefault("output_dir", str(Path.home() / "SmartScanATC" / "recordings"))
        atomic_write_text(path, json.dumps(raw, indent=2))
    except (OSError, ValueError):
        pass


def atomic_write_text(path: Path, text: str) -> None:
    """Write-then-rename, so a crash/power cut mid-write never leaves a
    truncated file behind - a half-written config.json used to load as
    "corrupt" -> silent defaults, which the next save() then made permanent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # newline="": csv already writes its own "\r\n" - no second translation on Windows.
    with temporary.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
    temporary.replace(path)


_migrate_from_smartscanatc()


def _from_dict(cls, raw: dict):
    """Builds a config dataclass ignoring keys it doesn't know - a config.json
    written by a newer/older version (or pushed by a newer desktop client to
    a remote receiver) must never crash load() with an unexpected-keyword
    TypeError. Also covers stale keys of removed options ("mode",
    "insert_marker", "audio_format")."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class DeviceConfig:
    driver: str = "auto"          # "auto", "rtlsdr", "simulated", or a SoapySDR driver key
    device_args: str = ""         # extra SoapySDR device args, e.g. "serial=00000001"
    sample_rate_hz: float = 2_400_000.0  # capture bandwidth; RTL-SDR handles ~2.4 MSPS
    # "manual" (fixed gain_db), "auto" (the tuner's own onboard hardware AGC),
    # or "sw_auto" (SoftwareAgc control loop - see agc.py, the default).
    # Hardware AGC can overdrive on a strong station (confirmed: +0.7dBFS,
    # past full scale, spurious detections on adjacent channels - AGENTS.md
    # gotcha 27); the owner's real-world use settled on software AGC.
    gain_mode: str = "sw_auto"

    gain_db: float = 30.0


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
    squelch_enabled: bool | None = None
    squelch_offset_db: float | None = None

    @property
    def has_advanced_overrides(self) -> bool:
        """True once at least one "zaawansowane" field above has actually
        been saved (the Listen page's per-band settings) - distinguishes a band
        that's merely *tagged* on a window (see ScanWindow.band) from one
        that's genuinely diverging from the global settings, e.g. for the
        spectrum view's trigger-line accent color."""
        return any((
            self.trigger_level_dbfs is not None, self.tuning_time_ms is not None,
            self.linger_time_ms is not None, self.gain_mode is not None,
            self.gain_db is not None, self.noise_reduction_enabled is not None,
            self.noise_reduction_strength is not None, self.squelch_enabled is not None,
            self.squelch_offset_db is not None,
        ))


@dataclass
class TimingConfig:
    tuning_time_ms: int = 60
    scanning_dwell_ms: int = 45
    linger_time_ms: int = 5000


@dataclass
class RecordingConfig:
    # Off by default: a fresh install shouldn't start writing audio to disk
    # before the user has deliberately opted in - see the "Auto-record"
    # toggles on the Listen and Settings pages (both control this field).
    enabled: bool = False
    # Auto-recording is always one file per transmission now - the old
    # "Single File (one continuous file)" mode never actually did that (see
    # git history: every new transmission opened a fresh Recorder/file
    # regardless of this setting, since each _Slot owns its own Recorder -
    # gotcha 12 - so it behaved like Split with a worse filename, no
    # metadata, and a pointless marker tone at the start of every file) -
    # removed rather than fixed, since manual recording (ScannerEngine.
    # start_manual_recording(), forced single-file internally via
    # Recorder's own single_file=True constructor flag, nothing to do with
    # this config) already covers the genuine "one continuous file"
    # use case correctly. `output_dir` below still applies to both.
    output_dir: str = DEFAULT_RECORDINGS_DIR
    # "wav" (default, always available) or "mp3" (requires ffmpeg on PATH -
    # see audio_transcode.py; falls back to keeping the WAV silently if
    # ffmpeg isn't installed, never loses the recording). WAV is still always
    # the real-time write target - transcoding only ever runs once a file is
    # fully closed (scanner_engine.py's _close_slot/stop_manual_recording),
    # never on the per-chunk recording path.
    output_format: str = "wav"
    # 0 = unlimited. When >0, ScannerEngine won't open a *new* auto-recording
    # slot once this many are already active simultaneously - the candidate
    # is simply reconsidered on a later tick once one of the existing ones
    # closes (LINGER expiry) and frees capacity again. Added as a safety
    # valve against a busy band (or one strong transmission's own spectral
    # leakage into tightly-spaced neighbour channels - see channelizer.py's
    # per-channel power window having no guard band) fragmenting into many
    # simultaneous slots/files at once. Only gates *recording* - has no
    # effect when `enabled` is False, and never caps how many channels can
    # be scanned/demodulated live (only one of them ever reaches the
    # speaker anyway - see ScannerEngine._open_slot's primary/weight logic).
    # Defaulted to 3 (not unlimited) - a sane out-of-the-box ceiling for a
    # fresh install, since unlimited is exactly what let the spectral-
    # leakage/fabricated-candidate bugs (see AGENTS.md gotcha 31/32) balloon
    # into a dozen-plus simultaneous recordings before those were found.
    max_concurrent_recordings: int = 3
    # Disk retention (see playback/index.py's Library.apply_retention) - 0 = off.
    retention_days: int = 0
    retention_max_gb: float = 0.0
    retention_keep_favorites: bool = True
    # Only ever auto-delete a recording some connected computer already
    # holds a verified copy of (see worker.py's mirroring).
    retention_require_mirrored: bool = False


@dataclass
class LocationConfig:
    """Receiver location - used only to build the FlightRadar24/ADSBExchange
    playback links on the Recordings page (an approximate location is plenty precise
    for a ~9-zoom flight-tracker map, not exact GPS coordinates). None means
    "not set yet" - see recordings.js (openMap),
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
    enabled: bool = True         # automatic background transcription of new recordings (see worker.py)
    generic_model: str = "large-v3"  # multilingual model (covers Polish transmissions, among others)
    english_atc_model: str = "jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper"
    device: str = "auto"         # "auto" | "cuda" | "cpu"
    compute_type: str = "auto"   # "auto" -> int8_float16 on GPU, int8 on CPU
    # Languages we bet the detection result on in uncertain cases (see
    # transcription_engine._resolve_language) - in this kind of listening,
    # only English and Polish realistically matter, so radio noise doesn't
    # get to confuse detection with exotic languages.
    candidate_languages: list[str] = field(default_factory=lambda: ["en", "pl"])
    min_duration_sec: float = 0.6  # shorter recordings (e.g. just a squelch crackle) are skipped


@dataclass
class RemoteReceiverConfig:
    """Client mode: this computer doesn't scan itself, it works for a
    FalconSDR server elsewhere - transcribing locally and/or keeping a
    local copy of its recordings (see worker.py). Off = local mode."""
    enabled: bool = False
    url: str = ""  # e.g. "http://receiver.local:8765"
    token: str = ""


@dataclass
class LegalDisclaimerConfig:
    """Which UI languages (see i18n.current_language()) have already seen
    each disclaimer - not a single "seen ever" flag, since dismissing it in
    one language doesn't mean it was actually read/understood in another
    the user (or a different person using the same install) later switches
    to. See gui/legal_disclaimer.py."""
    launch_seen_languages: list[str] = field(default_factory=list)
    recording_seen_languages: list[str] = field(default_factory=list)


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
            VfoRangeConfig(
                start_hz=118_000_000, stop_hz=136_975_000, step_hz=8_333,
                label="Civil aviation (118-137 MHz)",
            )
        ]
    )
    timing: TimingConfig = field(default_factory=TimingConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    recency_boost: RecencyBoostConfig = field(default_factory=RecencyBoostConfig)
    noise_reduction: NoiseReductionConfig = field(default_factory=NoiseReductionConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    remote_receiver: RemoteReceiverConfig = field(default_factory=RemoteReceiverConfig)
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
    # UI language - a code with a matching falconsdr/locale/<code>.po file,
    # "en" (the source language, built into every string, needs no file), or
    # "auto" (default for a fresh install - follow the OS's own language,
    # falling back to English if we have no translation for it). See
    # falconsdr/i18n.py's resolve_system_language()/set_language(). Applying
    # a change applies on the next page load (server log lines: right away).
    language: str = "auto"
    legal_disclaimer: LegalDisclaimerConfig = field(default_factory=LegalDisclaimerConfig)
    # Off by default - a real user shouldn't stumble into "Simulation (no
    # hardware)" in the device dropdown and wonder why the scanner is
    # hearing fake bursts instead of their actual RTL-SDR. Gates only the
    # device list's own "Simulation" entry (see Controller.devices()/
    # refresh_device_list()) - purely a UI visibility toggle, not a behavior
    # flag anything else in the app reads.
    debug_mode: bool = False
    # Optional password for the web UI and workers (Settings → Access).
    # Empty = anyone who can reach the server can use it.
    access_token: str = ""
    # Per-computer worker settings, keyed by worker name - see worker.py:
    # {"transcribe": bool, "mirror": bool, "mirror_dir": str}
    workers: dict = field(default_factory=dict)

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
            bands = [_from_dict(VfoRangeConfig, b) for b in raw["bands"]]
        else:
            # Migrate pre-band-list configs (single "vfo_range" + "extra_vfo_ranges").
            legacy_primary = raw.get("vfo_range")
            bands = [_from_dict(VfoRangeConfig, legacy_primary)] if legacy_primary else [VfoRangeConfig()]
            bands += [_from_dict(VfoRangeConfig, r) for r in raw.get("extra_vfo_ranges", [])]
        return AppConfig(
            device=_from_dict(DeviceConfig, raw.get("device", {})),
            bands=bands,
            timing=_from_dict(TimingConfig, raw.get("timing", {})),
            recording=_from_dict(RecordingConfig, raw.get("recording", {})),
            recency_boost=_from_dict(RecencyBoostConfig, raw.get("recency_boost", {})),
            noise_reduction=_from_dict(NoiseReductionConfig, raw.get("noise_reduction", {})),
            transcription=_from_dict(TranscriptionConfig, raw.get("transcription", {})),
            remote_receiver=_from_dict(RemoteReceiverConfig, raw.get("remote_receiver", {})),
            location=_from_dict(LocationConfig, raw.get("location", {})),
            trigger_level_dbfs=raw.get("trigger_level_dbfs", -30.0),
            squelch_enabled=raw.get("squelch_enabled", False),
            squelch_offset_db=raw.get("squelch_offset_db", -6.0),
            default_blacklist_margin_hz=raw.get("default_blacklist_margin_hz", 5000),
            memory_channel_bandwidth_hz=raw.get("memory_channel_bandwidth_hz", 12_500),
            channel_folders=list(raw.get("channel_folders", [])),
            memory_list_enabled=raw.get("memory_list_enabled", True),
            language=raw.get("language", "auto"),
            legal_disclaimer=_from_dict(LegalDisclaimerConfig, raw.get("legal_disclaimer", {})),
            debug_mode=raw.get("debug_mode", False),
            access_token=raw.get("access_token", ""),
            workers=dict(raw.get("workers", {})),
        )

    def save(self) -> None:
        atomic_write_text(CONFIG_PATH, json.dumps(asdict(self), indent=2, ensure_ascii=False))
