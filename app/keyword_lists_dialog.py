"""keyword_lists_dialog.py — Tools ▸ Keyword Lists: view the grooming and
drug lists with each term's source, switch terms on/off, and add or remove
the examiner's own terms (marked as theirs). The data lives in
keyword_lists.py; this is only the window."""

import getpass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QTableView, QVBoxLayout)

import keyword_lists as kl
from dialog_helpers import note_label, show_item_checkboxes

_COLUMNS = ("On", "Term", "Category", "Origin", "Source", "Off by default because", "Evidence")
_ORIGIN = {"baseline": "Baseline", "user": "Added by user", "llm": "AI-suggested, approved"}


class _TermsModel(QAbstractTableModel):
    def __init__(self, name: str):
        super().__init__()
        self.name = name
        self.rows: list = []
        self.reload()

    def reload(self) -> None:
        self.beginResetModel()
        self.rows = kl.load(self.name)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        t = self.rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if t.get("enabled") else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 1:
                return t.get("term", "")
            if col == 2:
                return t.get("category", "")
            if col == 3:
                origin = _ORIGIN.get(t.get("origin"), t.get("origin", ""))
                if t.get("origin") != "baseline" and t.get("added_by"):
                    origin += f" ({t['added_by']}, {t.get('added_at', '')})"
                return origin
            if col == 4:
                return t.get("source", "")
            if col == 5:
                return t.get("everyday_reason", "") if t.get("everyday") else ""
            if col == 6:
                return t.get("evidence", "")
        if role == Qt.ItemDataRole.ToolTipRole and col in (4, 6):
            return kl.sources(self.name).get(t.get("source", ""), t.get("evidence", ""))
        if role == Qt.ItemDataRole.UserRole:      # sort/filter key for column 0
            return 1 if t.get("enabled") else 0
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        return f

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            t = self.rows[index.row()]
            on = Qt.CheckState(value) == Qt.CheckState.Checked
            kl.set_enabled(self.name, kl.key(t), on)
            t["enabled"] = on
            self.dataChanged.emit(index, index)
            return True
        return False


class _Filter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.show = "all"

    def filterAcceptsRow(self, row, parent):
        t = self.sourceModel().rows[row]
        if self.show == "on" and not t.get("enabled"):
            return False
        if self.show == "off" and t.get("enabled"):
            return False
        if self.show == "added" and t.get("origin") == "baseline":
            return False
        if self.text:
            hay = " ".join(str(t.get(k, "")) for k in ("term", "category", "source", "evidence")).lower()
            return self.text in hay
        return True


class KeywordListsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyword Lists")
        self.resize(1100, 680)
        lay = QVBoxLayout(self)
        lay.addWidget(note_label(
            "Terms used to flag chats. The baseline lists come with FFS Explorer, each term with "
            "its source (hover the Source or Evidence column for the full reference). Terms "
            "that are ordinary everyday words are off by default because they give many harmless "
            "hits; switch any term on or off with its box. Terms you add are marked as yours. "
            "Matching is tolerant: disguised spellings such as p3d0, s.e.x, nuuudes, how old r u "
            "and one-letter typos of longer words also match."))
        top = QHBoxLayout()
        self.list_combo = QComboBox()
        for name in kl.LISTS:
            self.list_combo.addItem(kl.LIST_LABELS[name], name)
        top.addWidget(QLabel("List:"))
        top.addWidget(self.list_combo)
        self.show_combo = QComboBox()
        for label, val in (("All terms", "all"), ("On", "on"), ("Off", "off"), ("Added by user", "added")):
            self.show_combo.addItem(label, val)
        top.addWidget(self.show_combo)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter terms, categories, sources…")
        top.addWidget(self.filter_edit, 1)
        lay.addLayout(top)

        self.view = QTableView()
        show_item_checkboxes(self.view)
        self.view.setSortingEnabled(True)
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.view.verticalHeader().hide()
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        lay.addWidget(self.view, 1)

        self.count_label = QLabel()
        bottom = QHBoxLayout()
        bottom.addWidget(self.count_label, 1)
        add_btn = QPushButton("Add Term…")
        add_btn.clicked.connect(self._add)
        self.remove_btn = QPushButton("Remove My Term")
        self.remove_btn.setToolTip("Only terms you added can be removed; baseline terms can be switched off.")
        self.remove_btn.clicked.connect(self._remove)
        src_btn = QPushButton("Sources…")
        src_btn.clicked.connect(self._sources)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        for b in (add_btn, self.remove_btn, src_btn, close_btn):
            bottom.addWidget(b)
        lay.addLayout(bottom)

        self.list_combo.currentIndexChanged.connect(self._load)
        self.show_combo.currentIndexChanged.connect(self._refilter)
        self.filter_edit.textChanged.connect(self._refilter)
        self._load()

    def _name(self) -> str:
        return self.list_combo.currentData()

    def _load(self, *_):
        self.model = _TermsModel(self._name())
        self.proxy = _Filter()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.ItemDataRole.DisplayRole)
        self.view.setModel(self.proxy)
        self.model.dataChanged.connect(lambda *_: self._count())
        for col, w in ((0, 40), (1, 200), (2, 220), (3, 150), (4, 110), (5, 220)):
            self.view.setColumnWidth(col, w)
        self._refilter()

    def _refilter(self, *_):
        self.proxy.text = self.filter_edit.text().strip().lower()
        self.proxy.show = self.show_combo.currentData()
        self.proxy.invalidateFilter()
        self._count()

    def _count(self):
        rows = self.model.rows
        on = sum(1 for t in rows if t.get("enabled"))
        mine = sum(1 for t in rows if t.get("origin") != "baseline")
        self.count_label.setText(f"{len(rows):,} terms · {on:,} on · {mine:,} added by you · "
                                 f"{self.proxy.rowCount():,} shown")

    def _add(self):
        term, ok = QInputDialog.getText(self, "Add Term", "Term or phrase (end a word with * to "
                                        "also match longer words, e.g. delet*):")
        if not ok or not term.strip():
            return
        cats = sorted({t.get("category", "") for t in self.model.rows if t.get("category")})
        cat, ok = QInputDialog.getItem(self, "Add Term", "Category (or type a new one):",
                                       ["Added by user"] + cats, 0, True)
        if not ok:
            return
        note, ok = QInputDialog.getText(self, "Add Term", "Why (optional, e.g. case reference):")
        if not ok:
            return
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
        kl.add_term(self._name(), term.strip(), cat.strip() or "Added by user",
                    added_by=user, note=note.strip())
        self._load()

    def _remove(self):
        idx = self.view.currentIndex()
        if not idx.isValid():
            return
        t = self.model.rows[self.proxy.mapToSource(idx).row()]
        if t.get("origin") == "baseline":
            QMessageBox.information(self, "Keyword Lists",
                                    "Baseline terms can't be removed, so their source stays on "
                                    "record. Untick it to switch it off.")
            return
        if kl.remove_added(self._name(), kl.key(t)):
            self._load()

    def _sources(self):
        src = kl.sources(self._name())
        QMessageBox.information(self, "Sources",
                                "\n\n".join(f"{k}: {v}" for k, v in src.items()) or "No sources.")
