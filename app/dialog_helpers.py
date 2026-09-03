"""dialog_helpers.py — shared Qt dialog-construction helpers.

Extracted 2026-08-19 after a survey found the same shapes hand-rebuilt
repeatedly across the app: a button-row (QHBoxLayout + stretch +
Cancel/OK) at 25+ call sites, an explanatory note QLabel (wordWrap + a
note/warning style) at 24+, and FOUR different ad-hoc "something's wrong"
colors (`red`, `#c62828`, `#b00020`, `#b8860b`) used inconsistently
instead of one. WARNING_COLOR/ERROR_COLOR below reuse research_store.py's
existing `#b8860b`/`#c62828` rather than inventing new ones, so the whole
app converges on the same two colors, not a third and fourth.

Pure Qt-widget construction — no case/business logic — so any dialog
anywhere in the app can use these with no dependency on case state.
"""

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton

WARNING_COLOR = "#b8860b"   # amber — matches research_store.py's REVIEW_COLOR
ERROR_COLOR   = "#c62828"   # red — matches research_store.py's OUTCOME_COLORS["no_value"]
ACTIVE_COLOR  = "#1a73e8"   # blue — "this preset/mode is the one currently active"

NOTE_STYLE    = "color: grey; font-size: 11px;"
WARNING_STYLE = f"color: {WARNING_COLOR};"
ERROR_STYLE   = f"color: {ERROR_COLOR};"
# QPushButton stylesheet for "this is the currently-active preset" — e.g.
# the Artifact Viewer's Columns row highlighting whichever of All/Core
# exactly matches what's shown right now (see
# ArtifactViewerMixin._update_art_columns_indicator). Not a QLabel style
# like the three above — a button needs an explicit hover/pressed variant
# too or Qt's own style would otherwise repaint over the background on
# mouse-over.
ACTIVE_BUTTON_STYLE = (
    f"QPushButton {{ background-color: {ACTIVE_COLOR}; color: white; "
    f"font-weight: bold; }} "
    f"QPushButton:hover {{ background-color: {ACTIVE_COLOR}; color: white; }} "
    f"QPushButton:pressed {{ background-color: {ACTIVE_COLOR}; color: white; }}"
)


def error_label() -> QLabel:
    """An empty, error-styled label for in-form validation messages set
    later via .setText() (e.g. "Reason cannot be blank") — the shape
    repeated at several form dialogs, previously with three different
    literal red shades (`red`, `#c62828`, `#b00020`) between them; all
    converge on ERROR_COLOR now."""
    label = QLabel("")
    label.setStyleSheet(ERROR_STYLE)
    return label


def note_label(text: str, style: str = NOTE_STYLE) -> QLabel:
    """A word-wrapped explanatory label — the shape repeated 24+ times
    across the app (dialog caveats, per-field notes, warnings). Default
    style is the small grey "note" look; pass WARNING_STYLE or
    ERROR_STYLE for a more prominent caveat that needs to stand out."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(style)
    return label


def button_row(dlg: QDialog, ok_text: str = "OK", cancel_text: str = "Cancel",
               on_ok=None, on_cancel=None):
    """A right-aligned Cancel/OK-shaped button row — the shape repeated
    25+ times across the app, always in this same visual order (Cancel
    then the affirmative action). Returns (layout, cancel_btn, ok_btn) —
    add the layout to the dialog's own layout; the returned buttons let a
    caller adjust them further (disable until a form is valid, hide until
    some state is reached, etc).

    on_ok/on_cancel default to dlg.accept/dlg.reject — pass a callable
    when the affirmative action needs to run validation/save logic first
    (and possibly not close the dialog at all if it fails), which is the
    common case in this codebase's own dialogs more often than a bare
    accept(). Only fits a plain two-button row in this fixed order; a
    dialog with a third button or a different button order needs its own
    hand-built row rather than forcing this helper to fit."""
    layout = QHBoxLayout()
    layout.addStretch()
    cancel_btn = QPushButton(cancel_text)
    cancel_btn.clicked.connect(on_cancel or dlg.reject)
    ok_btn = QPushButton(ok_text)
    ok_btn.setDefault(True)
    ok_btn.clicked.connect(on_ok or dlg.accept)
    layout.addWidget(cancel_btn)
    layout.addWidget(ok_btn)
    return layout, cancel_btn, ok_btn
