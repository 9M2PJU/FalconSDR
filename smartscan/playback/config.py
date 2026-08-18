"""Ścieżki i stałe używane w zakładce odtwarzania (dawniej ATC Scanner Browser).

Katalog z nagraniami nie jest tu na stałe wpisany - odtwarzacz jest teraz
częścią SmartScanATC, więc zawsze korzysta z aktualnego
``AppConfig.recording.output_dir`` (patrz ``PlaybackPanel``). Stała
``SMARTSCAN_DIR`` poniżej to tylko wartość domyślna używana, gdyby ktoś
wywołał funkcje tego modułu bez podania własnego ``root``.
"""
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import DEFAULT_RECORDINGS_DIR

SMARTSCAN_DIR = Path(DEFAULT_RECORDINGS_DIR)

# Strefa czasowa, w której skaner zapisuje znaczniki czasu w nazwach plików
POLAND_TZ = ZoneInfo("Europe/Warsaw")
UTC_TZ = ZoneInfo("UTC")

# Katalog na dane aplikacji (cache jakości) - niezależny od katalogu z
# nagraniami, żeby nie zaśmiecać folderu skanera.
APP_DATA_DIR = Path.home() / ".local" / "share" / "atc_browser"
QUALITY_CACHE_FILE = APP_DATA_DIR / "quality_cache.json"
TRANSCRIPTION_CACHE_FILE = APP_DATA_DIR / "transcription_cache.json"
FAVORITES_FILE = APP_DATA_DIR / "favorites.json"

# Nazwa podfolderu z datą: format YYYY-MM-DD
DATE_DIR_FORMAT = "%Y-%m-%d"

# Wzorzec nazwy pliku: 2026-07-23_19-31-01-123456_134474.wav
# (data i godzina to CZAS POLSKI, opcjonalne -mmmmmm to mikrosekundy - dodane
# żeby dwie oddzielne transmisje na tej samej częstotliwości, zaczynające się
# w tej samej sekundzie zegarowej, nigdy nie dostały identycznej nazwy pliku
# i nie nadpisały się nawzajem (mikrosekundowa, nie milisekundowa,
# rozdzielczość - żeby to było matematycznie niemożliwe, nie tylko "bardzo
# mało prawdopodobne": dwa wywołania Pythona nie zdążą wykonać się w tej
# samej mikrosekundzie). Starsze nagrania bez tego segmentu nadal parsują
# się poprawnie, patrz parse_filename() w organizer.py - ostatnia liczba to
# zaokrąglona częstotliwość w kHz)
FILENAME_REGEX = r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})(?:-(\d{6}))?_(\d+)\.wav$"

APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
