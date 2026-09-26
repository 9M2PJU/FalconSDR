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

from dataclasses import dataclass

from .config import VfoRangeConfig
from .models import Channel, Modulation


def _ch(freq_mhz: float, name: str, modulation: Modulation = Modulation.AM) -> Channel:
    return Channel(freq_hz=int(round(freq_mhz * 1e6)), name=name, modulation=modulation, enabled=True)


# Wide sweeps (VFO range mode) --------------------------------------------
# Poland's civil airband (unchanged default) plus the two military bands.
# 8.33 kHz channel spacing is the European standard for civil aviation VHF
# voice since the 2018 mandate (down from the old 25 kHz raster), so that's
# the step this preset scans at.
CIVIL_BAND = VfoRangeConfig(start_hz=118_000_000, stop_hz=136_975_000, step_hz=8_333)
MILITARY_VHF_BAND = VfoRangeConfig(start_hz=137_000_000, stop_hz=143_975_000, step_hz=25_000)
MILITARY_UHF_BAND = VfoRangeConfig(start_hz=225_000_000, stop_hz=399_975_000, step_hz=25_000)

MILITARY_BANDS: list[VfoRangeConfig] = [MILITARY_VHF_BAND, MILITARY_UHF_BAND]
AVIATION_MILITARY_BANDS: list[VfoRangeConfig] = [MILITARY_VHF_BAND, MILITARY_UHF_BAND]  # extras; CIVIL_BAND is primary


@dataclass(frozen=True)
class BandPreset:
    """One entry in the "Add preset" picker for the band-list editor.
    ``label``/``tooltip`` here are the plain English source text - wrapped in
    ``tr()`` at the point of display (ScannerPanel._build_vfo_form), not
    here: this list is built once, at module-import time, which happens
    before ``i18n.set_language()`` ever runs (see main.py) - a tr() call
    made here would always resolve to English regardless of the configured
    language."""
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


# Preset catalogue offered in the Scanner tab's band-list editor. Picked for a
# roof-mounted wideband (discone-type) antenna + RTL-SDR v4: everything here
# sits in the VHF/UHF range the R828D tuner and that kind of antenna both
# handle well (~24 MHz and up), and each band has a realistic chance of
# catching real traffic rather than dead air.
BAND_PRESETS: list[BandPreset] = [
    BandPreset(
        id="civil_air", label="Civil aviation (118-137 MHz)",
        tooltip="Standard civil aviation band: towers, approach, area control.",
        start_hz=CIVIL_BAND.start_hz, stop_hz=CIVIL_BAND.stop_hz, step_hz=CIVIL_BAND.step_hz,
    ),
    BandPreset(
        id="mil_vhf", label="Military VHF (137-144 MHz)",
        tooltip="Military VHF band adjacent to civil aviation.",
        start_hz=MILITARY_VHF_BAND.start_hz, stop_hz=MILITARY_VHF_BAND.stop_hz,
        step_hz=MILITARY_VHF_BAND.step_hz,
    ),
    BandPreset(
        id="mil_uhf", label="Military UHF NATO (225-400 MHz)",
        tooltip=(
            "Harmonized NATO UHF band: air-to-air refueling,\n"
            "QRA/Baltic Air Policing, exercises."
        ),
        start_hz=MILITARY_UHF_BAND.start_hz, stop_hz=MILITARY_UHF_BAND.stop_hz,
        step_hz=MILITARY_UHF_BAND.step_hz,
    ),
    BandPreset(
        id="pmr446", label="PMR446 (446.00625-446.19375 MHz)",
        tooltip=(
            "License-free PMR446 walkie-talkies (16 analog channels, NFM).\n"
            "Very good chance of catching something nearby - popular on\n"
            "trips, construction sites, at hotels/campsites."
        ),
        start_hz=446_006_250, stop_hz=446_193_750, step_hz=12_500, modulation="NFM",
    ),
    BandPreset(
        id="ham_2m", label="Ham 2m (144-146 MHz)",
        tooltip="2m amateur radio band (VHF, NFM): repeaters and simplex.",
        start_hz=144_000_000, stop_hz=146_000_000, step_hz=12_500, modulation="NFM",
    ),
    BandPreset(
        id="ham_70cm", label="Ham 70cm (430-440 MHz)",
        tooltip="70cm amateur radio band (UHF, NFM): repeaters and simplex.",
        start_hz=430_000_000, stop_hz=440_000_000, step_hz=12_500, modulation="NFM",
    ),
    BandPreset(
        id="marine_vhf", label="Marine VHF (156-162 MHz)",
        tooltip=(
            "Marine VHF band (channels 1-88, including channel 16 - calling/\n"
            "distress, NFM). Useful near a coast, harbor, or a lake with shipping traffic."
        ),
        start_hz=156_000_000, stop_hz=162_025_000, step_hz=25_000, modulation="NFM",
    ),
    BandPreset(
        id="fm_broadcast", label="FM broadcast radio (87.5-108 MHz)",
        tooltip=(
            "FM broadcast band (WFM). Not ATC, but strong, always-on stations\n"
            "make it easy to quickly check the antenna/receiver works correctly."
        ),
        start_hz=87_500_000, stop_hz=108_000_000, step_hz=100_000, modulation="WFM",
    ),
    BandPreset(
        id="fire_vhf", label="Fire brigade (148.6625-149.3375 MHz)",
        tooltip=(
            "PSP/OSP VHF band (NFM), home to the whole rescue-firefighting\n"
            "channel plan (KRG) and the OSP siren-alerting network. Many OSP\n"
            "units still operate on analog despite PSP's ongoing migration to\n"
            "DMR - see also the ready-made KRG channels in the Channels tab\n"
            "(\"Load fire brigade\"). Poland-specific."
        ),
        start_hz=148_662_500, stop_hz=149_337_500, step_hz=12_500, modulation="NFM",
    ),
    BandPreset(
        id="prm", label="Emergency medical / PRM (168.5-169.2 MHz)",
        tooltip=(
            "State medical rescue service (ambulances / PRM), power-grid\n"
            "emergency service, and municipal services. Ambulances and PRM\n"
            "dispatchers in many regions still transmit here in analog.\n"
            "Poland-specific."
        ),
        start_hz=168_500_000, stop_hz=169_200_000, step_hz=12_500, modulation="NFM",
    ),
]

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
