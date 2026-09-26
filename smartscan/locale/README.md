# Adding a translation

SmartScanATC's UI strings are looked up from plain-text `.po` files in this
directory - the standard GNU gettext format, the same one tools like
[Poedit](https://poedit.net/) already understand. There is no compile step
and no extra tool to install: a `.po` file is read directly at runtime (see
`smartscan/i18n.py`).

## Adding a new language (e.g. Slovak)

1. Copy `pl.po` to `sk.po` in this directory.
2. Translate every `msgstr "..."` line. Leave every `msgid "..."` line
   untouched - that's the original English text, used as the lookup key.
3. Open `smartscan/i18n.py` and add your language's code and display name to
   the `LANGUAGE_NAMES` dict, e.g. `"sk": "Slovenčina"`. This step is
   optional (the language shows up in Options either way, just as its raw
   code instead of a nice name if you skip it).

Restart the app - your language now appears in Options → Language.

## Updating an existing translation

Just edit the `.po` file directly - `msgstr` is the only part translators
ever need to touch. A missing or empty `msgstr` for some `msgid` simply
falls back to showing the English text for that one string, so a partial or
in-progress translation never breaks anything.

## `.po` format quick reference

```
msgid "Scanner"
msgstr "Skaner"
```

A long string can be split across several lines by writing it as adjacent
quoted strings (gettext's own convention, and what Poedit produces
automatically for long lines):

```
msgid "A very long string that "
"continues on the next source line"
msgstr "..."
```

The header block at the top of the file (`msgid ""` / `msgstr ""` /
`"Content-Type: ..."`) is boilerplate expected by gettext-aware tools like
Poedit - keep it, but there's nothing in it you need to translate.
