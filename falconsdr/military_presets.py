"""Curated military/NATO aviation, fire-brigade and emergency-services
frequencies for Poland with a realistic chance of catching real traffic
(tankers, Baltic Air Policing/QRA, exercises near the eastern flank; PSP/OSP
rescue-firefighting dispatch; PRM ambulance dispatch). All aviation voice
channels use AM, same as civil aviation; fire-brigade/PRM channels use NFM.

Sources are public monitoring references (NATO Joint Civil/Military Frequency
Agreement UHF band 225-400 MHz, published guard/common-use frequencies, Polish
military airbase tower/approach frequencies, the nationwide PSP "Kanal
Ratowniczo-Gasniczy" (KRG) channel plan plus the OSP siren-alerting frequency
pair, and the 168.5-169.2 MHz band used by PRM/pogotowie ratunkowe dispatch)
as documented by hobbyist aviation/PMR-monitoring sites. Real-world usage
shifts with exercises, re-assignments and PSP/OSP's ongoing migration to
digital DMR (many OSP units still run analog, but not all), so treat this as a
good starting point, not a guarantee - add anything you personally confirm
active via the Memory List tab.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .config import CONFIG_DIR, VfoRangeConfig, atomic_write_text
from .models import Channel, Modulation

logger = logging.getLogger(__name__)


def _ch(freq_mhz: float, name: str, modulation: Modulation = Modulation.AM) -> Channel:
    return Channel(freq_hz=int(round(freq_mhz * 1e6)), name=name, modulation=modulation, enabled=True)


@dataclass(frozen=True)
class BandPreset:
    """One entry in the "Add preset" picker for the band-list editor.
    ``label``/``tooltip`` here are the plain English source text - wrapped in
    ``t()`` at the point of display (the web UI), not
    here: presets are loaded once, at module-import time (or whenever
    reload_presets()/save_user_presets() runs), which can happen before
    ``i18n.set_language()`` ever runs (see main.py) - a tr() call made here
    would always resolve to English regardless of the configured language.
    Deliberately plain, JSON-serializable field types only (str/int) - see
    to_dict()/from_dict()."""
    id: str
    label: str
    tooltip: str
    start_hz: int
    stop_hz: int
    step_hz: int
    modulation: str = "AM"  # Modulation.value - AM for aviation, NFM/WFM for the rest

    def to_band(self) -> VfoRangeConfig:
        return VfoRangeConfig(
            start_hz=self.start_hz, stop_hz=self.stop_hz, step_hz=self.step_hz,
            label=self.label, enabled=True, modulation=self.modulation,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "BandPreset":
        return BandPreset(
            id=str(d["id"]), label=str(d["label"]), tooltip=str(d.get("tooltip", "")),
            start_hz=int(d["start_hz"]), stop_hz=int(d["stop_hz"]), step_hz=int(d["step_hz"]),
            modulation=str(d.get("modulation", "AM")),
        )


# Built-in preset catalogue, shipped with the app as a plain, human-editable
# JSON file (not hardcoded here) - punkt 9/finalization request: presets
# should be easy for a user to inspect, tweak, back up and share, not
# something only a code change can touch. Picked to be useful in most
# countries by default (see AGENTS.md's own notes on this) - no country-
# specific bands here; those live as opt-in *discrete channels*
# (MILITARY_CHANNELS/FIRE_CHANNELS below) instead, loaded only on request.
_BUILTIN_PRESETS_PATH = Path(__file__).resolve().parent / "presets" / "band_presets.json"
# User's own additions/edits/hides - never touched by an app update (that
# only ever replaces _BUILTIN_PRESETS_PATH, which ships inside the package),
# and never lost by one either. Absent entirely on a fresh install - see
# load_user_data()'s own empty-defaults fallback. A user CAN edit or delete
# a built-in preset (explicit request - "once someone renames it, it's
# theirs from then on") - see get_all_presets()'s docstring for exactly how
# that's represented without ever touching the built-in file itself.
USER_PRESETS_PATH = CONFIG_DIR / "band_presets.json"


def _load_user_data_raw(path: Path) -> tuple[list[BandPreset], set[str]]:
    if not path.exists():
        return [], set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            # Pre-"delete built-ins" format (this feature's own first cut) -
            # just a plain list of presets, no hidden-ids concept yet.
            return [BandPreset.from_dict(d) for d in raw], set()
        presets = [BandPreset.from_dict(d) for d in raw.get("presets", [])]
        hidden = set(raw.get("hidden_builtin_ids", []))
        return presets, hidden
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Couldn't read presets from %s: %s", path, exc)
        return [], set()


def load_builtin_presets() -> list[BandPreset]:
    if not _BUILTIN_PRESETS_PATH.exists():
        return []
    try:
        raw = json.loads(_BUILTIN_PRESETS_PATH.read_text(encoding="utf-8"))
        return [BandPreset.from_dict(d) for d in raw]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Couldn't read built-in presets from %s: %s", _BUILTIN_PRESETS_PATH, exc)
        return []


def load_user_presets() -> list[BandPreset]:
    presets, _hidden = _load_user_data_raw(USER_PRESETS_PATH)
    return presets


def load_hidden_builtin_ids() -> set[str]:
    _presets, hidden = _load_user_data_raw(USER_PRESETS_PATH)
    return hidden


def save_user_data(presets: list[BandPreset], hidden_builtin_ids: set[str]) -> None:
    atomic_write_text(USER_PRESETS_PATH, json.dumps(
        {"presets": [p.to_dict() for p in presets], "hidden_builtin_ids": sorted(hidden_builtin_ids)},
        indent=2, ensure_ascii=False,
    ))


def save_user_presets(presets: list[BandPreset]) -> None:
    """Convenience wrapper keeping the current hidden-ids set untouched -
    every write from the presets editor that isn't a delete-a-built-in goes
    through this one instead of building the tuple by hand each time."""
    save_user_data(presets, load_hidden_builtin_ids())


def hide_builtin_preset(preset_id: str) -> None:
    """"Deletes" a built-in preset - the built-in JSON file itself is never
    touched (it's part of the installed package, restored by every update
    regardless), so a deletion is recorded here instead and filtered out by
    get_all_presets(). Restored by "Reset to defaults" (Controller.reset_to_defaults) clearing
    this whole file, or by re-importing/re-adding a preset with the same id
    (which shadows it back "visible" again via the same mechanism as an
    edit - see get_all_presets())."""
    hidden = load_hidden_builtin_ids()
    hidden.add(preset_id)
    save_user_data(load_user_presets(), hidden)


def get_all_presets() -> list[BandPreset]:
    """Built-ins (minus any hidden or shadowed - see below) followed by the
    user's own - called fresh at every point of use (the web UI's preset
    picker and editor) rather than cached in a module-level
    constant, so an edit made in the presets editor shows up immediately
    without needing a restart - the exact same "function, not a frozen
    constant" fix AGENTS.md already documents for the i18n footgun, applied
    here for a different reason (live edits, not translation) but the same
    underlying shape.

    Editing or deleting a *built-in* preset (both explicitly requested -
    "once someone renames it, it's theirs from then on") never touches
    _BUILTIN_PRESETS_PATH (part of the installed package - every app update
    would just restore it anyway). Both are represented purely in the
    user's own file instead:
    - **Edit**: upsert_preset() saves the edited version into the user
      presets list *under the same id* the built-in had. A built-in whose
      id also appears in the user list is excluded here - the user version
      "shadows" it - so from that point on it's indistinguishable from (and
      editable/deletable exactly like) a preset the user created from
      scratch, matching the explicit request.
    - **Delete**: the id goes into hidden_builtin_ids instead (see
      hide_builtin_preset()) and is excluded here too, with no
      corresponding user-list entry - "gone", not "shadowed by nothing"."""
    user_presets = load_user_presets()
    hidden = load_hidden_builtin_ids()
    user_ids = {p.id for p in user_presets}
    visible_builtins = [p for p in load_builtin_presets() if p.id not in hidden and p.id not in user_ids]
    return visible_builtins + user_presets


def unique_preset_id(label: str, existing_ids: set[str]) -> str:
    slug = "".join(c.lower() if c.isalnum() else "_" for c in label).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    base = candidate = slug or "preset"
    i = 2
    while candidate in existing_ids:
        candidate = f"{base}_{i}"
        i += 1
    return candidate


def upsert_preset(preset: BandPreset) -> BandPreset:
    """Adds (empty id -> a fresh unique one) or edits a preset. Editing a
    pure built-in saves it into the user file under the same id, which
    shadows the built-in from then on (see get_all_presets())."""
    user_presets = load_user_presets()
    if not preset.id:
        preset = replace(preset, id=unique_preset_id(preset.label, {p.id for p in get_all_presets()}))
    if any(p.id == preset.id for p in user_presets):
        user_presets = [preset if p.id == preset.id else p for p in user_presets]
    else:
        user_presets.append(preset)
    save_user_presets(user_presets)
    return preset


def delete_preset(preset_id: str) -> None:
    """Removes a user-owned preset, or hides a pure built-in - the two take
    different paths since only a user-owned one has an entry to remove."""
    if is_user_owned_preset(preset_id):
        save_user_presets([p for p in load_user_presets() if p.id != preset_id])
    else:
        hide_builtin_preset(preset_id)


def import_presets(raw: list[dict]) -> int:
    """Never overwrites on an id collision - an imported preset whose id is
    already taken gets a fresh unique id instead. Raises on malformed input."""
    imported = [BandPreset.from_dict(d) for d in raw]
    user_presets = load_user_presets()
    existing_ids = {p.id for p in get_all_presets()}
    for preset in imported:
        if preset.id in existing_ids:
            preset = replace(preset, id=unique_preset_id(preset.label, existing_ids))
        existing_ids.add(preset.id)
        user_presets.append(preset)
    save_user_presets(user_presets)
    return len(imported)


def is_user_owned_preset(preset_id: str) -> bool:
    """True once a preset (built-in-sourced originally or not) has a user-
    file entry under this id - i.e. it's editable/deletable via
    upsert_preset()/delete_preset(). A still-pure built-in (never edited) is False."""
    return preset_id in {p.id for p in load_user_presets()}


# Curated discrete channels (Memory List mode) ------------------------------
# Highest hit-rate picks: guard/emergency, NATO/USAF air-to-air refueling
# (tankers), Warsaw OAT military control sectors, and the eastern/northern
# QRA and reinforcement bases most active during Baltic Air Policing and
# eastern-flank exercises. Channel names are kept as their real, Poland-
# specific facility/callsign names (e.g. "Malbork Wieza/Tower") rather than
# translated - they're proper nouns, the actual identifiers heard on air, not
# generic UI text.
MILITARY_CHANNELS: list[Channel] = [
    # -- Guard / emergency (always worth a slot, near-constant monitoring) --
    _ch(243.000, "UHF Guard (MAD, emergency)"),
    _ch(121.500, "VHF Guard (IAD, emergency)"),

    # -- NATO/USAF common air-to-air refueling (tankers) --
    _ch(282.000, "Tankowanie - AR Common"),
    _ch(282.800, "Tankowanie - AR Common"),
    _ch(294.800, "Tankowanie - USAF Air Refueling"),
    _ch(299.600, "Tankowanie - AR Common"),
    _ch(341.700, "Tankowanie - AR Primary (boom coord.)"),
    _ch(349.700, "Tankowanie - AR Secondary (boom coord.)"),
    _ch(282.100, "SAR / Search and Rescue"),

    # -- EPWW (Warsaw) OAT - military in-flight control sectors --
    _ch(378.050, "EPWW OAT GF/B (UHF)"),
    _ch(278.600, "EPWW OAT GF/NE (UHF)"),
    _ch(267.825, "EPWW OAT NE/C (UHF)"),
    _ch(280.725, "EPWW OAT JR (UHF)"),
    _ch(339.850, "EPWW OAT DT/B (UHF)"),
    _ch(143.500, "EPWW OAT GF/NE/JR/C sekundarna (VHF)"),
    _ch(139.000, "EPWW OAT DT/B sekundarna (VHF)"),

    # -- Malbork EPMB (QRA, Baltic Air Policing) --
    _ch(235.200, "Malbork PAR (UHF)"),
    _ch(240.550, "Malbork Approach (UHF)"),
    _ch(234.050, "Malbork Wieza/Tower (UHF)"),

    # -- Lask EPLK (F-16, BAP) --
    _ch(306.775, "Lask ATIS (UHF)"),

    # -- Minsk Mazowiecki EPMM (eastern flank, deployment/refueling) --
    _ch(278.575, "Minsk Mazowiecki Approach (UHF)"),
    _ch(279.075, "Minsk Mazowiecki Wieza/Tower (UHF)"),

    # -- Powidz EPPW (strategic transport, tankers) --
    _ch(279.850, "Powidz ATIS (UHF)"),

    # -- Swidwin EPSN --
    _ch(234.800, "Swidwin PAR (UHF)"),
    _ch(278.975, "Swidwin Approach (UHF)"),
    _ch(233.975, "Swidwin Wieza/Tower (UHF)"),

    # -- Miroslawiec EPMI (UAVs/drones, support) --
    _ch(234.075, "Miroslawiec PAR (UHF)"),
    _ch(234.875, "Miroslawiec Approach (UHF)"),
    _ch(264.875, "Miroslawiec Wieza/Tower (UHF)"),
]

# Fire brigade (PSP/OSP) - nationwide rescue-firefighting channels (KRG) from
# the VHF frequency plan (148.6625-149.3375 MHz, NFM, 12.5 kHz raster) plus
# the OSP siren selective-alerting frequency pair. KRG 1-8 are the channels
# used nationwide during rescue-firefighting operations (not local dispatch
# desks), so they're the ones most likely to carry real traffic. Many OSP
# units still operate on analog despite PSP's ongoing migration to DMR -
# other/additional channels from the same band may be in local use.
FIRE_CHANNELS: list[Channel] = [
    _ch(148.7000, "PSP KRG 1 (nationwide)", Modulation.NFM),
    _ch(149.0875, "PSP KRG 2 (nationwide)", Modulation.NFM),
    _ch(149.1375, "PSP KRG 3 (nationwide)", Modulation.NFM),
    _ch(149.1625, "PSP KRG 4 (nationwide)", Modulation.NFM),
    _ch(149.2250, "PSP KRG 5 (nationwide)", Modulation.NFM),
    _ch(149.2375, "PSP KRG 6 (nationwide)", Modulation.NFM),
    _ch(149.2625, "PSP KRG 7 (nationwide)", Modulation.NFM),
    _ch(149.2750, "PSP KRG 8 (nationwide)", Modulation.NFM),
    _ch(148.7250, "OSP - siren alerting (Tx)", Modulation.NFM),
    _ch(148.8250, "OSP - siren alerting (Rx)", Modulation.NFM),
]
