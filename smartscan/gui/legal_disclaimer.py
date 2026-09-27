"""Legal notices about listening to and recording radio transmissions.

Shown once per UI *language* (see AGENTS.md gotcha 20), not once ever -
dismissing this in English doesn't mean it was ever actually read by someone
using the app in Polish (or the same person switching languages later), and
the whole point is that the user has actually seen it in a language they
understand. `AppConfig.legal_disclaimer` (LegalDisclaimerConfig) tracks which
languages have already seen which of the two notices below; callers
(MainWindow) are responsible for persisting the config after either function
returns something that changed it.
"""
from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from ..config import LegalDisclaimerConfig
from ..i18n import current_language, tr


def maybe_show_launch_disclaimer(parent: QWidget, config: LegalDisclaimerConfig) -> bool:
    """Shows the general startup notice if the current language hasn't seen
    it yet. Purely informational (a single acknowledgement button, not a
    yes/no gate) - it doesn't block using the app, just makes sure this was
    put in front of the user at least once per language before they start.
    Returns True if it was shown (config changed, caller should persist)."""
    lang = current_language()
    if lang in config.launch_seen_languages:
        return False
    QMessageBox.information(
        parent,
        tr("Before you start"),
        tr(
            "SmartScanATC can tune to a wide range of radio frequencies, "
            "some of which may be restricted or illegal to listen to or "
            "record where you live.\n\n"
            "Using this software does not authorize you to listen to or "
            "record any transmission. Radio monitoring and recording laws "
            "vary significantly between countries - some places allow "
            "listening to most frequencies but restrict recording or "
            "sharing what you hear; others restrict listening to non-public "
            "or certain public-safety/aviation communications entirely.\n\n"
            "You are solely responsible for knowing and following the laws "
            "that apply to you. If you're unsure, check your local "
            "telecommunications regulations before using the scanning or "
            "recording features."
        ),
    )
    config.launch_seen_languages.append(lang)
    return True


def confirm_recording_disclaimer(parent: QWidget, config: LegalDisclaimerConfig) -> bool:
    """Shows a more pointed notice the first time (per language) the user
    is actually about to start recording anything - auto-recording, the
    manual Record button, or raw IQ recording all funnel through this, since
    the underlying legal question ("are you allowed to record this") is the
    same for all three. Unlike the launch notice, this one is a real gate:
    returns False if the user declines, and the caller must then abort
    whatever recording action triggered it (revert the toggle/button that
    was just clicked) rather than starting anyway. Returns True immediately,
    with no dialog, once already accepted in the current language."""
    lang = current_language()
    if lang in config.recording_seen_languages:
        return True
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(tr("Before you record"))
    box.setText(tr(
        "You're about to record for the first time.\n\n"
        "Recording radio transmissions can be more restricted than simply "
        "listening to them, depending on your country's laws - even where "
        "monitoring itself is legal. Make sure you've checked the rules "
        "that apply where you live before continuing."
    ))
    continue_btn = box.addButton(tr("I understand, continue"), QMessageBox.ButtonRole.AcceptRole)
    box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(continue_btn)
    box.exec()
    accepted = box.clickedButton() is continue_btn
    if accepted:
        config.recording_seen_languages.append(lang)
    return accepted
