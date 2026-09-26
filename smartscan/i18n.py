"""UI translation (internationalization).

Every user-facing string in the source code is written in English and passed
through ``tr()``. Translations live as plain-text ``.po`` files (the standard
GNU gettext format - the same one tools like Poedit, Weblate or Crowdin
already understand) under ``smartscan/locale/<lang>.po``.

Deliberately NOT using Python's ``gettext`` module directly: that requires a
compiled binary ``.mo`` file (built from the ``.po`` source via the external
``msgfmt`` tool), which is one more technical step between "someone writes a
translation" and "the app can use it" - exactly the kind of friction that
turns away a translator who doesn't already have a C toolchain / gettext
utilities installed (this matters a lot on Windows/macOS, where ``msgfmt``
isn't preinstalled). Instead, ``.po`` is parsed directly, at runtime, by the
small parser below. Anyone can add a new language by writing one text file
and nothing else - no build step, no compiler, no extra dependency.

To add a new language (e.g. Slovak):
  1. Copy ``smartscan/locale/pl.po`` to ``smartscan/locale/sk.po``.
  2. Translate every ``msgstr "..."`` line (leave ``msgid`` alone - that's
     the original English text, used as the lookup key).
  3. Add the language to ``LANGUAGE_NAMES`` below so it shows up with a
     proper display name in Options, instead of just its raw code.
That's the whole process - restart the app and it appears in the language
list automatically (``available_languages()`` just lists whatever ``.po``
files exist).

Changing the active language takes effect on the *next* restart, not live:
every widget's text is set once, at construction time, from whatever
``tr()`` returned back then - there's no app-wide "retranslate every already-
built widget" mechanism (that's a much bigger feature - Qt calls this a
LanguageChange event + a retranslateUi() convention - and isn't worth the
complexity for a desktop tool nobody switches language in mid-session).
"""
from __future__ import annotations

import re
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent / "locale"

# Display names for the Options language dropdown - a language whose .po
# file exists but isn't listed here still works, it just shows its raw code
# (e.g. "sk") instead of a nice name until someone adds an entry here.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "pl": "Polski",
}

_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _unescape(raw: str) -> str:
    out = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            out.append(_ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


_STRING_LINE = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*$')


def parse_po(text: str) -> dict[str, str]:
    """Parses the subset of the .po format this project actually produces:
    ``msgid``/``msgstr`` pairs, each value possibly continued across several
    quoted-string lines (gettext's convention for long strings), ``#``
    comment lines ignored, blank lines separating entries. Good enough for
    hand-written or Poedit-exported files without needing a real gettext
    library - this is a translation *catalog* reader, not a validator; a
    malformed line is skipped rather than raising, so one bad entry in a
    community-contributed file can't break the whole language.
    """
    catalog: dict[str, str] = {}
    msgid: list[str] | None = None
    msgstr: list[str] | None = None
    target: str | None = None  # "id" or "str" - which list new string lines append to

    def _flush() -> None:
        if msgid is not None and msgstr is not None:
            key = "".join(msgid)
            value = "".join(msgstr)
            if key and value:
                catalog[key] = value

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("msgid "):
            _flush()
            msgid, msgstr, target = [], None, "id"
            m = _STRING_LINE.match(stripped[len("msgid "):])
            if m:
                msgid.append(_unescape(m.group(1)))
            continue
        if stripped.startswith("msgstr "):
            msgstr, target = [], "str"
            m = _STRING_LINE.match(stripped[len("msgstr "):])
            if m:
                msgstr.append(_unescape(m.group(1)))
            continue
        m = _STRING_LINE.match(stripped)
        if m and target == "id" and msgid is not None:
            msgid.append(_unescape(m.group(1)))
        elif m and target == "str" and msgstr is not None:
            msgstr.append(_unescape(m.group(1)))
    _flush()
    return catalog


def available_languages() -> list[str]:
    """Every language with a .po file present, plus "en" (the source
    language, needs no file - tr() just returns its input unchanged)."""
    langs = ["en"]
    if LOCALE_DIR.is_dir():
        langs += sorted(p.stem for p in LOCALE_DIR.glob("*.po"))
    return langs


def language_display_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


_catalog: dict[str, str] = {}
_current_language = "en"


def set_language(lang: str) -> None:
    """Loads ``smartscan/locale/<lang>.po`` into the active catalog. "en"
    (or any language with no .po file, e.g. a code that got typed wrong in
    config.json) means "no translation" - tr() then just echoes its input,
    which is already English, so this is always a safe, silent fallback."""
    global _catalog, _current_language
    _current_language = lang
    path = LOCALE_DIR / f"{lang}.po"
    if lang == "en" or not path.exists():
        _catalog = {}
        return
    try:
        _catalog = parse_po(path.read_text(encoding="utf-8"))
    except OSError:
        _catalog = {}


def current_language() -> str:
    return _current_language


def tr(text: str) -> str:
    """Translates ``text`` (English source) to the active language, or
    returns it unchanged if there's no catalog entry - a missing/partial
    translation degrades to English for just that one string rather than
    breaking anything, so a translation file can be added to incrementally."""
    return _catalog.get(text, text)
