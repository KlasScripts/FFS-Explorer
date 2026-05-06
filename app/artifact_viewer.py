"""artifact_viewer.py — ArtifactRunnerWorker, ArtifactRunnerDialog, and ArtifactViewerMixin."""

import os
import pathlib
import zipfile
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QLabel, QLineEdit, QComboBox, QHBoxLayout, QVBoxLayout,
    QTableView, QTreeView, QPlainTextEdit, QStackedWidget, QSplitter,
    QHeaderView, QScrollArea, QCheckBox, QPushButton, QDialog, QMessageBox,
)
from PySide6.QtCore import Qt, QThread, Signal, QSortFilterProxyModel
from PySide6.QtGui import QStandardItemModel, QStandardItem, QFont

from db_utils import _open_results_db

# ── Tree node role sentinels ──────────────────────────────────────────────────
# Each item in the artifact tree stores one of these prefixes + the script name
# as its UserRole data.  The click handler strips the prefix to determine what
# action to take and which script to act on.
_ART_GROUP  = "__art_group__:"
_ART_REPORT = "__art_report__:"
_ART_SCRIPT = "__art_script__:"
_ART_SOURCE = "__art_source__:"
_ART_FILES  = "__art_files__:"


class ArtifactRunnerWorker(QThread):
    log  = Signal(str)
    done = Signal()

    def __init__(self, selected, zip_path, adapter, streaming_index, case_dir):
        super().__init__()
        self._selected        = selected        # [(script_name, module), ...]
        self._zip_path        = zip_path
        self._adapter         = adapter
        self._streaming_index = streaming_index
        self._case_dir        = case_dir

    def run(self):
        from artifact_runner import run_artifact
        from artifact_db import write_artifact_results

        try:
            case_conn = _open_results_db(self._case_dir)
        except Exception as exc:
            self.log.emit(f"Could not open results database: {exc}")
            self.done.emit()
            return

        zip_obj = None
        if self._streaming_index is None:
            try:
                zip_obj = zipfile.ZipFile(self._zip_path, 'r')
            except Exception as exc:
                self.log.emit(f"Could not open archive: {exc}")
                case_conn.close()
                self.done.emit()
                return

        try:
            for script_name, module in self._selected:
                label = getattr(module, 'name', script_name)
                self.log.emit(f"Running: {label}…")
                rows, error = run_artifact(
                    script_name, module,
                    self._zip_path, self._adapter,
                    case_dir=self._case_dir,
                    zip_obj=zip_obj,
                    streaming_index=self._streaming_index,
                )
                if error:
                    self.log.emit(f"  Error: {error}")
                else:
                    count = write_artifact_results(case_conn, script_name, rows)
                    self.log.emit(f"  Done — {count} rows written.")
        except Exception as exc:
            self.log.emit(f"\nUnexpected error: {exc}")
        finally:
            case_conn.close()
            if zip_obj:
                zip_obj.close()

        self.log.emit("\nAll selected parsers finished.")
        self.done.emit()


class ArtifactRunnerDialog(QDialog):
    parsers_completed = Signal()

    def __init__(self, zip_path, zip_names, adapter, streaming_index, case_dir,
                 is_android, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Run Artifact Parsers")
        self.setMinimumSize(480, 540)
        self._worker = None

        platform = 'android' if is_android else 'ios'

        from artifact_runner import list_artifacts
        all_artifacts = list_artifacts(platform)

        def _exists(candidates):
            if streaming_index is not None:
                return any(c in streaming_index for c in candidates)
            return any(c in zip_names for c in candidates)

        def _mod_matches(mod):
            if hasattr(mod, 'app_path') and hasattr(mod, 'files'):
                app_base = mod.app_path.strip('/')
                return any(
                    _exists(adapter.user_candidates(f"{app_base}/{sub.lstrip('/')}"))
                    for sub in mod.files.values()
                )
            return any(
                _exists(adapter.user_candidates(ui_path))
                for ui_path in getattr(mod, 'target_paths', [])
            )

        available = [
            (script_name, mod)
            for script_name, mod in all_artifacts
            if _mod_matches(mod)
        ]

        layout = QVBoxLayout(self)

        if not available:
            layout.addWidget(QLabel("No artifact parsers matched files in the loaded archive."))
            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.reject)
            layout.addWidget(close_btn)
            return

        layout.addWidget(QLabel(f"Parsers matched ({platform.upper()}) — select to run:"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(6, 6, 6, 6)
        inner_layout.setSpacing(5)
        self._checkboxes: list[tuple[QCheckBox, str, object]] = []
        for script_name, mod in available:
            cb = QCheckBox(getattr(mod, 'name', script_name))
            cb.setChecked(True)
            inner_layout.addWidget(cb)
            self._checkboxes.append((cb, script_name, mod))
        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        layout.addWidget(QLabel("Log:"))
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(170)
        self._log.setFont(QFont("Courier", 10))
        layout.addWidget(self._log)

        btn_row = QHBoxLayout()
        self._run_btn   = QPushButton("Run")
        self._close_btn = QPushButton("Close")
        self._run_btn.clicked.connect(self._run)
        self._close_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        self._zip_path        = zip_path
        self._adapter         = adapter
        self._streaming_index = streaming_index
        self._case_dir        = case_dir

    def _run(self):
        selected = [
            (sn, mod)
            for cb, sn, mod in self._checkboxes
            if cb.isChecked()
        ]
        if not selected:
            return

        self._run_btn.setEnabled(False)
        self._close_btn.setEnabled(False)
        self._log.clear()

        self._worker = ArtifactRunnerWorker(
            selected, self._zip_path, self._adapter,
            self._streaming_index, self._case_dir,
        )
        self._worker.log.connect(self._log.appendPlainText)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self):
        self._run_btn.setEnabled(True)
        self._close_btn.setEnabled(True)
        self.parsers_completed.emit()

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)


class ArtifactViewerMixin:
    """Mixin providing the Artifact Viewer tab and runner dialog for FastZipBrowser."""

    def _setup_artifact_tab(self) -> QWidget:
        """Build the Artifact Viewer tab — own tree on the left, content on the right."""
        self._art_tree_model = QStandardItemModel()
        self._art_tree_model.setHorizontalHeaderLabels(["Device Artifacts"])

        self._art_tree_view = QTreeView()
        self._art_tree_view.setModel(self._art_tree_model)
        self._art_tree_view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._art_tree_view.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self._art_tree_view.setHeaderHidden(False)
        self._art_tree_view.setMinimumWidth(200)
        self._art_tree_view.setMaximumWidth(320)
        self._art_tree_view.clicked.connect(self._on_art_tree_clicked)

        # ── Right side: stacked widget ────────────────────────────────────────
        self._art_placeholder = QLabel("Select a Report or Script from the tree.")
        self._art_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._art_placeholder.setStyleSheet("color: grey; font-style: italic;")

        # Report page — filter bar + QTableView with proxy
        report_page = QWidget()
        report_layout = QVBoxLayout(report_page)
        report_layout.setContentsMargins(0, 0, 0, 0)
        report_layout.setSpacing(4)

        report_filter_row = QHBoxLayout()
        self._art_filter_input = QLineEdit()
        self._art_filter_input.setPlaceholderText("Filter…")
        self._art_filter_col  = QComboBox()
        self._art_filter_col.addItem("All Columns")
        self._art_filter_input.textChanged.connect(self._apply_art_filter)
        self._art_filter_col.currentIndexChanged.connect(self._apply_art_filter)
        self._art_row_label = QLabel()
        report_filter_row.addWidget(QLabel("Filter:"))
        report_filter_row.addWidget(self._art_filter_input, 1)
        report_filter_row.addWidget(self._art_filter_col)
        report_filter_row.addWidget(self._art_row_label)
        report_layout.addLayout(report_filter_row)

        self._art_report_model = QStandardItemModel()
        self._art_report_proxy = QSortFilterProxyModel()
        self._art_report_proxy.setSourceModel(self._art_report_model)
        self._art_report_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._art_report_proxy.setFilterKeyColumn(-1)

        self._art_report_view = QTableView()
        self._art_report_view.setModel(self._art_report_proxy)
        self._art_report_view.setSortingEnabled(True)
        self._art_report_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._art_report_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._art_report_view.setAlternatingRowColors(True)
        self._art_report_view.setWordWrap(True)
        self._art_report_view.horizontalHeader().setStretchLastSection(True)
        self._art_report_view.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        report_layout.addWidget(self._art_report_view)

        # Script page — read-only monospace text editor
        self._art_script_view = QPlainTextEdit()
        self._art_script_view.setReadOnly(True)
        self._art_script_view.setFont(QFont("Courier", 11))

        self._art_stack = QStackedWidget()
        self._art_stack.addWidget(self._art_placeholder)  # 0
        self._art_stack.addWidget(report_page)             # 1
        self._art_stack.addWidget(self._art_script_view)  # 2

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._art_tree_view)
        splitter.addWidget(self._art_stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        tab = QWidget()
        QHBoxLayout(tab).addWidget(splitter)
        tab.layout().setContentsMargins(4, 4, 4, 4)
        return tab

    def _refresh_artifact_tab(self):
        """Rebuild the artifact tree from completed parsers in the case DB."""
        if not hasattr(self, '_art_tree_model'):
            return
        from artifact_db import list_completed_artifacts
        from artifact_runner import list_artifacts

        self._art_tree_model.clear()
        self._art_tree_model.setHorizontalHeaderLabels(["Device Artifacts"])
        self._art_report_model.clear()
        self._art_stack.setCurrentIndex(0)   # back to placeholder

        if not self._case_dir:
            return
        try:
            case_conn = _open_results_db(self._case_dir)
            completed = list_completed_artifacts(case_conn)
            case_conn.close()
        except Exception:
            return

        if not completed:
            placeholder = QStandardItem("No parsers run yet")
            placeholder.setEditable(False)
            self._art_tree_model.invisibleRootItem().appendRow(placeholder)
            return

        platform = 'android' if self._is_android_archive() else 'ios'
        modules  = {sn: mod for sn, mod in list_artifacts(platform)}

        def _item(text, role_val=None):
            it = QStandardItem(text)
            it.setEditable(False)
            it.setCheckable(False)
            if role_val is not None:
                it.setData(role_val, Qt.ItemDataRole.UserRole)
            return it

        for script_name in completed:
            mod   = modules.get(script_name)
            label = getattr(mod, 'name', script_name) if mod else script_name
            group = _item(label, _ART_GROUP + script_name)
            group.setFont(QFont("Arial", weight=QFont.Weight.Bold))
            group.appendRow(_item("Report",        _ART_REPORT + script_name))
            group.appendRow(_item("Script",        _ART_SCRIPT + script_name))
            group.appendRow(_item("Source in ZIP", _ART_SOURCE + script_name))
            group.appendRow(_item("Exported Files",_ART_FILES  + script_name))
            self._art_tree_model.invisibleRootItem().appendRow(group)

        self._art_tree_view.expandAll()

    def _on_art_tree_clicked(self, index):
        role_val = index.data(Qt.ItemDataRole.UserRole)
        if not role_val:
            return
        if role_val.startswith(_ART_REPORT):
            self._art_show_report(role_val[len(_ART_REPORT):])
        elif role_val.startswith(_ART_SCRIPT):
            self._art_show_script(role_val[len(_ART_SCRIPT):])
        elif role_val.startswith(_ART_SOURCE):
            self._art_goto_source(role_val[len(_ART_SOURCE):])
        elif role_val.startswith(_ART_FILES):
            self._art_show_files(role_val[len(_ART_FILES):])

    def _art_resize_columns(self, max_width: int = 320):
        """Size columns to their content, then cap any that exceed max_width.
        Capped columns will word-wrap their text across multiple row lines."""
        self._art_report_view.resizeColumnsToContents()
        for col in range(self._art_report_model.columnCount()):
            if self._art_report_view.columnWidth(col) > max_width:
                self._art_report_view.setColumnWidth(col, max_width)

    def _art_show_report(self, script_name: str):
        from artifact_db import load_artifact_results
        try:
            case_conn = _open_results_db(self._case_dir)
            columns, rows = load_artifact_results(case_conn, script_name)
            case_conn.close()
        except Exception as exc:
            self.status_bar.showMessage(f"Could not load report: {exc}")
            return

        self._art_report_model.clear()
        self._art_filter_input.clear()
        self._art_filter_col.blockSignals(True)
        self._art_filter_col.clear()
        self._art_filter_col.addItem("All Columns")
        for col in columns:
            self._art_filter_col.addItem(col)
        self._art_filter_col.blockSignals(False)

        self._art_report_model.setHorizontalHeaderLabels(columns)
        for row in rows:
            self._art_report_model.appendRow(
                [QStandardItem(str(v) if v is not None else "") for v in row]
            )
        self._art_resize_columns()
        self._art_row_label.setText(f"{len(rows):,} rows")
        self._art_stack.setCurrentIndex(1)

    def _apply_art_filter(self):
        text    = self._art_filter_input.text()
        col_idx = self._art_filter_col.currentIndex() - 1  # -1 = all columns
        self._art_report_proxy.setFilterKeyColumn(col_idx)
        self._art_report_proxy.setFilterFixedString(text)
        visible = self._art_report_proxy.rowCount()
        total   = self._art_report_model.rowCount()
        self._art_row_label.setText(
            f"{visible:,} of {total:,} rows" if text else f"{total:,} rows"
        )

    def _art_show_script(self, script_name: str):
        from artifact_runner import _ARTIFACTS_DIR
        platform    = 'android' if self._is_android_archive() else 'ios'
        script_path = os.path.join(_ARTIFACTS_DIR, platform, f"{script_name}.py")
        if not os.path.isfile(script_path):
            self.status_bar.showMessage(f"Script not found: {script_path}")
            return
        try:
            text = pathlib.Path(script_path).read_text(encoding='utf-8')
        except Exception as exc:
            self.status_bar.showMessage(f"Could not read script: {exc}")
            return
        self._art_script_view.setPlainText(text)
        self._art_stack.setCurrentIndex(2)
        self.status_bar.showMessage(f"Script: {script_path}")

    def _art_goto_source(self, script_name: str):
        """Switch to File Browser and navigate to the target folder in the zip."""
        from artifact_runner import list_artifacts
        platform = 'android' if self._is_android_archive() else 'ios'
        modules  = {sn: mod for sn, mod in list_artifacts(platform)}
        mod      = modules.get(script_name)
        if not mod:
            return
        if hasattr(mod, 'app_path') and hasattr(mod, 'files'):
            first_sub = next(iter(mod.files.values()), None)
            if not first_sub:
                return
            first_path = mod.app_path.strip('/') + '/' + first_sub.lstrip('/')
        else:
            target_paths = getattr(mod, 'target_paths', [])
            if not target_paths:
                return
            first_path = target_paths[0]
        parent = '/'.join(first_path.split('/')[:-1])
        self.center_tabs.setCurrentIndex(0)   # switch to File Browser
        self.navigate_tree_to_path(parent)

    def _art_show_files(self, script_name: str):
        """Show exported source files as a simple report table."""
        from artifact_runner import list_artifacts, safe_folder_name
        platform    = 'android' if self._is_android_archive() else 'ios'
        modules     = {sn: mod for sn, mod in list_artifacts(platform)}
        mod         = modules.get(script_name)
        parser_name = getattr(mod, 'name', script_name) if mod else script_name
        folder      = os.path.join(self._case_dir, 'artifact_parser_files',
                                   safe_folder_name(parser_name))
        if not os.path.isdir(folder):
            self.status_bar.showMessage(f"No exported files for {parser_name}")
            return

        columns = ["Name", "Size (Bytes)", "Modified", "Full Path"]
        self._art_report_model.clear()
        self._art_filter_input.clear()
        self._art_filter_col.blockSignals(True)
        self._art_filter_col.clear()
        self._art_filter_col.addItem("All Columns")
        for col in columns:
            self._art_filter_col.addItem(col)
        self._art_filter_col.blockSignals(False)
        self._art_report_model.setHorizontalHeaderLabels(columns)

        rows = []
        for fname in sorted(os.listdir(folder)):
            fpath = os.path.join(folder, fname)
            if not os.path.isfile(fpath):
                continue
            sz    = os.path.getsize(fpath)
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime('%Y-%m-%d %H:%M:%S')
            self._art_report_model.appendRow([
                QStandardItem(fname), QStandardItem(f"{sz:,}"),
                QStandardItem(mtime), QStandardItem(fpath),
            ])
            rows.append(fname)
        self._art_resize_columns()
        self._art_row_label.setText(f"{len(rows)} file(s)")
        self._art_stack.setCurrentIndex(1)
        self.status_bar.showMessage(f"Exported files — {folder}")

    def _open_artifact_runner(self):
        if not self.zip_path:
            return
        if not self._case_dir:
            QMessageBox.information(self, "No Case Folder",
                                    "A case folder is required to store results.\n"
                                    "Use File → Process Archive… to set one first.")
            return
        dlg = ArtifactRunnerDialog(
            zip_path=self.zip_path,
            zip_names=self.zip_names,
            adapter=self._adapter,
            streaming_index=self._streaming_index,
            case_dir=self._case_dir,
            is_android=self._is_android_archive(),
            parent=self,
        )
        dlg.parsers_completed.connect(self._refresh_artifact_tab)
        dlg.exec()
