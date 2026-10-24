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

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy, QStyleFactory

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


def show_item_checkboxes(view) -> None:
    """Give an item view (QListWidget/QTreeView/...) the Fusion style so its
    item checkboxes are drawn. On macOS, QMacStyle draws an item's tick only
    while that row is selected (ASSUMPTIONS.md A-61) — reproduced 2026-10-21
    on a Columns-dialog list: only the current row showed a tick.

    One style object PER VIEW, parented to it, so it lives exactly as long as
    the view. A single shared style was tried first and was wrong: Qt deleted
    it along with the first dialog's list, and every later dialog then
    crashed with "Internal C++ object (QCommonStyle) already deleted"."""
    style = QStyleFactory.create("Fusion")
    if style is not None:
        style.setParent(view)
        view.setStyle(style)


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


_GROWS = (QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)


class FlowLayout(QLayout):
    """Lays its items out left to right and wraps onto further lines when
    the width runs out (after Qt's own Flow Layout example). Added
    2026-10-24 for the File Browser's filter bar: as one QHBoxLayout its 15
    controls needed 1,241 px, and since a QTabWidget is as wide as its widest
    page, that forced the whole window to a 1,610 px minimum — wider than a
    MacBook screen, so maximising clipped the right of every tab."""

    def __init__(self, parent=None, h_spacing=8, v_spacing=4):
        super().__init__(parent)
        self._items = []
        self._h, self._v = h_spacing, v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self):
        w = sum(i.sizeHint().width() for i in self._items) + self._h * max(0, len(self._items) - 1)
        h = max((i.sizeHint().height() for i in self._items), default=0)
        m = self.contentsMargins()
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, apply):
        """Place items line by line; returns the height used. On each line,
        spare width goes to items whose widget expands horizontally (the
        filter text box), so a wide window still gets a wide text box."""
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        lines, line, used = [], [], 0
        for item in self._items:
            w = item.sizeHint().width()
            if line and used + self._h + w > r.width():
                lines.append(line)
                line, used = [], 0
            used += (self._h if line else 0) + w
            line.append(item)
        if line:
            lines.append(line)
        y = r.y()
        for line in lines:
            hints = [i.sizeHint() for i in line]
            spare = max(0, r.width() - sum(h.width() for h in hints) - self._h * (len(line) - 1))
            grow = [n for n, i in enumerate(line) if i.widget() is not None and
                    i.widget().sizePolicy().horizontalPolicy() in _GROWS]
            line_h = max(h.height() for h in hints)
            x = r.x()
            for n, (item, hint) in enumerate(zip(line, hints)):
                w = hint.width() + (spare // len(grow) if n in grow else 0)
                if apply:
                    item.setGeometry(QRect(QPoint(x, y + (line_h - hint.height()) // 2), QSize(w, hint.height())))
                x += w + self._h
            y += line_h + self._v
        return (y - self._v if lines else y) - rect.y() + m.bottom()
