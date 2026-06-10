"""highlight_delegate.py — shared item delegate that highlights a search/filter term."""

import html
import re

from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate
from PySide6.QtGui import QTextDocument
from PySide6.QtCore import Qt


class HighlightDelegate(QStyledItemDelegate):
    """Renders cells with the active term highlighted in yellow.

    get_term — callable returning the current term ('' disables highlighting)
    column   — restrict highlighting to a single column index (None = all columns)
    """

    _HL_BG = "#ffeb3b"
    _HL_FG = "#000000"

    def __init__(self, get_term, column: int | None = None, parent=None):
        super().__init__(parent)
        self._get_term = get_term
        self._column = column

    def paint(self, painter, option, index):
        if self._column is not None and index.column() != self._column:
            super().paint(painter, option, index)
            return

        text = index.data(Qt.ItemDataRole.DisplayRole) or ''
        term = self._get_term()
        if not term or not text:
            super().paint(painter, option, index)
            return

        self.initStyleOption(option, index)

        style = option.widget.style() if option.widget else QApplication.style()
        option.text = ''
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)

        escaped = html.escape(text)
        pattern = re.compile(re.escape(html.escape(term)), re.IGNORECASE)
        highlighted = pattern.sub(
            lambda m: (f'<span style="background:{self._HL_BG};color:{self._HL_FG};">'
                       f'{m.group()}</span>'),
            escaped
        )

        if option.state & QStyle.StateFlag.State_Selected:
            fg = option.palette.highlightedText().color().name()
        else:
            fg = option.palette.text().color().name()

        doc = QTextDocument()
        doc.setDefaultStyleSheet(f'body {{ color: {fg}; }}')
        doc.setHtml(f'<body>{highlighted}</body>')
        doc.setTextWidth(option.rect.width())

        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, option, option.widget)

        painter.save()
        painter.translate(text_rect.topLeft())
        painter.setClipRect(text_rect.translated(-text_rect.topLeft()))
        doc.drawContents(painter)
        painter.restore()
