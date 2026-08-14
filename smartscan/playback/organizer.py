"""Segregacja nagrań: przenoszenie luźnych plików .wav z katalogu głównego
skanera do podfolderów wg daty (czas polski zawarty w nazwie pliku)."""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

from smartscan.playback.config import DATE_DIR_FORMAT, FILENAME_REGEX, POLAND_TZ, SMARTSCAN_DIR

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(FILENAME_REGEX)


@dataclass
class ParsedName:
    dt_local: datetime
    freq_khz: int


def parse_filename(name: str) -> ParsedName | None:
    """Wyciąga datę/godzinę (czas polski) i częstotliwość (kHz) z nazwy pliku.
    Mikrosekundy (patrz FILENAME_REGEX) są opcjonalne - stare nazwy plików
    sprzed tej zmiany parsują się dalej, po prostu z zerowymi mikrosekundami."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    year, month, day, hour, minute, second, micros, freq = m.groups()
    dt_local = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second),
        microsecond=int(micros) if micros else 0,
        tzinfo=POLAND_TZ,
    )
    return ParsedName(dt_local=dt_local, freq_khz=int(freq))


def organize(root: Path = SMARTSCAN_DIR) -> List[Path]:
    """Przenosi pliki .wav leżące luzem bezpośrednio w `root` do podfolderów
    `root/YYYY-MM-DD/` odpowiadających polskiej dacie z nazwy pliku.

    Zwraca listę nowych ścieżek przeniesionych plików. Operacja jest
    bezpieczna do wielokrotnego uruchamiania (idempotentna) - pliki, które są
    już w podfolderach, nie są ruszane.
    """
    root = Path(root)
    if not root.is_dir():
        logger.warning("Katalog %s nie istnieje", root)
        return []

    moved: List[Path] = []

    loose_files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".wav"]

    for path in sorted(loose_files):
        parsed = parse_filename(path.name)
        if parsed is None:
            logger.debug("Pomijam plik o nierozpoznanej nazwie: %s", path.name)
            continue

        date_dir = root / parsed.dt_local.strftime(DATE_DIR_FORMAT)
        date_dir.mkdir(exist_ok=True)
        dest = date_dir / path.name

        if dest.exists():
            if dest.resolve() == path.resolve():
                continue
            # Nazwa już zajęta przez inny plik (nie powinno się zdarzyć, bo
            # znacznik czasu w nazwie ma rozdzielczość sekundy) - dopisz sufiks
            # zamiast nadpisywać lub gubić dane.
            stem, suffix = dest.stem, dest.suffix
            i = 1
            while dest.exists():
                dest = date_dir / f"{stem}_dup{i}{suffix}"
                i += 1
            logger.warning("Kolizja nazw, zapisuję jako %s", dest.name)

        shutil.move(str(path), str(dest))
        moved.append(dest)
        logger.info("Przeniesiono %s -> %s", path.name, dest.relative_to(root))

    return moved


def iter_all_recordings(root: Path = SMARTSCAN_DIR):
    """Zwraca ścieżki wszystkich plików .wav w katalogu głównym oraz we
    wszystkich podfolderach z datą (posortowane po nazwie, czyli chronologicznie)."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(root.glob("**/*.wav"))
