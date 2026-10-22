"""Paths and constants used by the playback tab (formerly ATC Scanner Browser).

The recordings directory isn't hardcoded here - the player is now part of
FalconSDR, so it always uses the current ``AppConfig.recording.output_dir``
(see ``index.Library``). The ``FALCONSDR_DIR`` constant below is only the
default value used if something calls this module's functions without
passing its own ``root``.
"""
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import DEFAULT_RECORDINGS_DIR

FALCONSDR_DIR = Path(DEFAULT_RECORDINGS_DIR)

# Time zone the scanner writes filename timestamps in.
POLAND_TZ = ZoneInfo("Europe/Warsaw")
UTC_TZ = ZoneInfo("UTC")

# Application data directory (quality cache) - separate from the recordings
# directory, so it doesn't clutter the scanner's own folder.
APP_DATA_DIR = Path.home() / ".local" / "share" / "FalconSDR"
_OLD_APP_DATA_DIR = Path.home() / ".local" / "share" / "atc_browser"  # name until 2026-10
if not APP_DATA_DIR.exists() and _OLD_APP_DATA_DIR.is_dir():
    import shutil
    shutil.copytree(_OLD_APP_DATA_DIR, APP_DATA_DIR)  # copy: the old folder stays as a backup
QUALITY_CACHE_FILE = APP_DATA_DIR / "quality_cache.json"
TRANSCRIPTION_CACHE_FILE = APP_DATA_DIR / "transcription_cache.json"
FAVORITES_FILE = APP_DATA_DIR / "favorites.json"

# Date subfolder name format: YYYY-MM-DD
DATE_DIR_FORMAT = "%Y-%m-%d"

# Filename pattern: 2026-07-23_19-31-01-123456_134474.wav
# (date and time are LOCAL TIME; the optional -mmmmmm is microseconds - added
# so two separate transmissions on the same frequency starting within the
# same clock second never get an identical filename and overwrite each other
# (microsecond, not millisecond, resolution - so this is mathematically
# impossible, not just "very unlikely": two Python statements can't execute
# within the same microsecond). Older recordings without this segment still
# parse correctly, see parse_filename() in organizer.py - the last number is
# the rounded frequency in kHz.
#
# Also matches recorder.py's "single" mode naming (manual recording, and the
# global "Single File" option) - literally "..._session.wav", no frequency
# at all, since that file isn't tied to one fixed channel - see
# organizer.parse_filename()'s handling (models.MANUAL_FREQ_KHZ). Without
# this alternative, every such file was silently invisible to the whole
# recording library (organize() skipped it forever, scan_recordings()
# dropped it) despite being a perfectly real, playable recording on disk -
# a real bug a user hit (manual Record "did nothing" - it did, the file just
# never showed up anywhere).
#
# Also matches .mp3 (audio_transcode.py, gotcha 14) - the extension carries
# no meaning for parsing beyond "which player/decoder to use", so it's a
# plain alternative here, not a separate capture group.
FILENAME_REGEX = r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})(?:-(\d{6}))?_(?:(\d+)|session)\.(?:wav|mp3)$"

APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
