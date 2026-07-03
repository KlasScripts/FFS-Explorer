"""segb_viewer.py — SegbViewerMixin: a viewer for Apple SEGB record files.

SEGB ("segmented buffer") is the record container Apple uses across many iOS
data stores (Biome streams, etc.).  Each record is almost always a **protobuf
message** whose schema isn't published.

Records are parsed with the vendored, MIT-licensed ``ccl_segb`` library and the
payloads decoded with ``blackboxprotobuf``.  Decoding is improved by a **schema**
(see ``segb_schemas``) chosen by the stream's path:
  * built-in schemas were ported from iLEAPP's ``biome*`` scripts (field types,
    names, and timestamp/GUID rendering hints);
  * a user can refine or create a schema when the auto-guess is wrong, and
    **save it into the case database** so it auto-applies next time.

When no schema matches, payloads are decoded schema-lessly with analysis aids:
candidate timestamp interpretations for numbers, a UTF-8 preview and nested
decode for byte fields, and JSON export.

Honest limitations: with no schema, field numbers are opaque and the inferences
are heuristics for analyst review, not authoritative.  Records marked Deleted may
be partial and can fail to decode (shown as raw hex).
"""

import datetime
import io
import json
from contextlib import closing

from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QSplitter, QTableView,
    QTreeView, QPushButton, QFileDialog, QHeaderView, QAbstractItemView,
    QDialog, QPlainTextEdit, QDialogButtonBox, QMessageBox, QCheckBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItemModel, QStandardItem, QColor, QBrush

from ccl_segb import ccl_segb1, ccl_segb2
from ccl_segb.ccl_segb_common import EntryState
from segb_schemas import stream_key_for_path, get_schema

_DELETED_BG = QColor(198, 40, 40, 70)   # red-ish, matches the SQLite diff view

_TS_MIN = datetime.datetime(2008, 1, 1)
_TS_MAX = datetime.datetime(2040, 1, 1)
_COCOA_EPOCH = datetime.datetime(2001, 1, 1)
_UNIX_EPOCH = datetime.datetime(1970, 1, 1)


def is_segb(data: bytes) -> bool:
    """True if *data* looks like a SEGB file by its header magic (v2 at offset
    0; v1 at offset 52, the end of its 56-byte header)."""
    return data[:4] == b'SEGB' or (len(data) >= 56 and data[52:56] == b'SEGB')


def _read_segb_records(data: bytes):
    bio = io.BytesIO(data)
    if ccl_segb1.stream_matches_segbv1_signature(bio):
        return list(ccl_segb1.read_segb1_stream(bio)), 'v1'
    bio.seek(0)
    if ccl_segb2.stream_matches_segbv2_signature(bio):
        return list(ccl_segb2.read_segb2_stream(bio)), 'v2'
    return [], None


def _bbp():
    try:
        import blackboxprotobuf
        return blackboxprotobuf
    except Exception:
        return None


def _render_epoch(hint: str, value) -> str:
    epoch = _COCOA_EPOCH if hint == 'cocoa' else _UNIX_EPOCH
    try:
        dt = epoch + datetime.timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError, OSError):
        return ''
    return f'{dt:%Y-%m-%d %H:%M:%S}'


def _timestamp_hints(value) -> str:
    hints = []
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ''
    for label, epoch, secs in (
        ('Unix', _UNIX_EPOCH, v), ('Unix ms', _UNIX_EPOCH, v / 1_000),
        ('Mac/Cocoa', _COCOA_EPOCH, v), ('Cocoa ns', _COCOA_EPOCH, v / 1_000_000_000),
    ):
        try:
            dt = epoch + datetime.timedelta(seconds=secs)
        except (OverflowError, OSError, ValueError):
            continue
        if _TS_MIN <= dt <= _TS_MAX:
            hints.append(f'{label}: {dt:%Y-%m-%d %H:%M:%S}')
    return f'   ⏱ {"  |  ".join(hints)}' if hints else ''


def _printable(b: bytes, limit: int = 80) -> str:
    if not b:
        return ''
    try:
        s = b.decode('utf-8')
    except UnicodeDecodeError:
        return ''
    flat = s.replace('\n', ' ').replace('\t', ' ').replace('\r', ' ')
    if not flat.isprintable():
        return ''
    return (flat[:limit] + '…') if len(flat) > limit else flat


def _sorted_keys(d: dict):
    return sorted(d, key=lambda k: (0, int(k)) if str(k).isdigit() else (1, str(k)))


def _hex_preview(b: bytes, limit: int = 96) -> str:
    return b[:limit].hex(' ') + ('  …' if len(b) > limit else '')


def _json_default(o):
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    if isinstance(o, datetime.datetime):
        return o.isoformat()
    return str(o)


class SegbViewerMixin:

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _setup_segb_tab(self) -> QWidget:
        self._segb_all: list = []            # every parsed record
        self._segb_view: list = []           # [(orig_index, record)] currently shown
        self._segb_show_deleted = False
        self._segb_name = ''
        self._segb_stream_key = None
        self._segb_schema = None
        self._segb_schema_source = 'none'

        self._segb_record_model = QStandardItemModel()
        self._segb_record_model.setHorizontalHeaderLabels(
            ['#', 'Timestamp', 'State', 'Size', 'CRC'])
        self._segb_table_view = QTableView()
        self._segb_table_view.setModel(self._segb_record_model)
        self._segb_table_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._segb_table_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._segb_table_view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._segb_table_view.verticalHeader().setVisible(False)
        self._segb_table_view.horizontalHeader().setStretchLastSection(True)
        self._segb_table_view.selectionModel().currentRowChanged.connect(
            lambda cur, _prev: self._on_segb_record_selected(cur.row()))

        self._segb_schema_label = QLabel("Schema: —")
        self._segb_deleted_check = QCheckBox("Show deleted")
        self._segb_deleted_check.setToolTip(
            "Also list records the SEGB marked as deleted (off by default — "
            "the view focuses on live records).")
        self._segb_deleted_check.toggled.connect(self._on_segb_show_deleted_toggled)
        self._segb_deleted_check.setVisible(False)
        self._segb_edit_btn = QPushButton("Edit schema…")
        self._segb_edit_btn.clicked.connect(self._edit_segb_schema)
        self._segb_edit_btn.setEnabled(False)
        self._segb_export_btn = QPushButton("Export JSON")
        self._segb_export_btn.clicked.connect(self._export_segb_json)
        self._segb_export_btn.setEnabled(False)

        top = QWidget()
        top_l = QVBoxLayout(top)
        top_l.setContentsMargins(0, 0, 0, 0)
        top_l.setSpacing(2)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(QLabel("Records"))
        bar.addWidget(self._segb_schema_label)
        bar.addStretch(1)
        bar.addWidget(self._segb_deleted_check)
        bar.addWidget(self._segb_edit_btn)
        bar.addWidget(self._segb_export_btn)
        top_l.addLayout(bar)
        top_l.addWidget(self._segb_table_view, stretch=1)

        self._segb_tree_model = QStandardItemModel()
        self._segb_tree_model.setHorizontalHeaderLabels(['Field', 'Type', 'Value'])
        self._segb_tree_view = QTreeView()
        self._segb_tree_view.setModel(self._segb_tree_model)
        self._segb_tree_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._segb_tree_view.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self._segb_tree_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        self._segb_status_label = QLabel("No SEGB file selected")

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(splitter, stretch=1)
        layout.addWidget(self._segb_status_label)
        return tab

    # ── Loading ────────────────────────────────────────────────────────────────

    def _load_segb_preview(self, ui_path: str, data: bytes) -> None:
        self._clear_segb_preview()
        self._segb_name = ui_path.rsplit('/', 1)[-1]
        try:
            records, version = _read_segb_records(data)
        except Exception as exc:
            self._segb_status_label.setText(f"{self._segb_name} — could not parse SEGB: {exc}")
            return
        if version is None:
            self._segb_status_label.setText(f"{self._segb_name} is not a SEGB file.")
            return

        self._segb_all = records
        self._segb_version = version
        self._segb_resolve_schema(ui_path)
        self._segb_refresh_record_list()

    @staticmethod
    def _segb_is_empty(rec) -> bool:
        """Empty records carry no usable payload — never shown."""
        return rec.state == EntryState.Unknown or len(rec.data) == 0

    def _segb_refresh_record_list(self) -> None:
        """Rebuild the record list: live records always, deleted only when the
        toggle is on, empty records never."""
        live_n = sum(1 for r in self._segb_all
                     if r.state == EntryState.Written and not self._segb_is_empty(r))
        deleted_n = sum(1 for r in self._segb_all
                        if r.state == EntryState.Deleted and not self._segb_is_empty(r))

        view = []
        for i, rec in enumerate(self._segb_all):
            if self._segb_is_empty(rec):
                continue
            if rec.state == EntryState.Deleted and not self._segb_show_deleted:
                continue
            view.append((i, rec))
        self._segb_view = view

        sel = self._segb_table_view.selectionModel()
        sel.blockSignals(True)
        self._segb_record_model.removeRows(0, self._segb_record_model.rowCount())
        for orig_i, rec in view:
            state = rec.state.name if isinstance(rec.state, EntryState) else str(rec.state)
            crc = ('OK' if rec.crc_passed else 'FAIL') if rec.state == EntryState.Written else '—'
            cells = [str(orig_i), str(getattr(rec, 'timestamp1', '')), state, str(len(rec.data)), crc]
            items = []
            for text in cells:
                it = QStandardItem(text)
                it.setEditable(False)
                if rec.state == EntryState.Deleted:
                    it.setBackground(QBrush(_DELETED_BG))
                items.append(it)
            self._segb_record_model.appendRow(items)
        sel.blockSignals(False)

        self._segb_deleted_check.setVisible(deleted_n > 0)
        self._segb_deleted_check.setText(f"Show deleted ({deleted_n})")
        self._segb_export_btn.setEnabled(bool(view))
        self._segb_edit_btn.setEnabled(bool(view) and _bbp() is not None)

        bbp_note = '' if _bbp() else '  ·  install blackboxprotobuf for record decoding'
        extra = f' (+{deleted_n} deleted)' if (deleted_n and not self._segb_show_deleted) else ''
        self._segb_status_label.setText(
            f"{self._segb_name} — SEGB {self._segb_version} · {live_n} live "
            f"record{'' if live_n == 1 else 's'}{extra}{bbp_note}")

        if view:
            self._segb_table_view.selectRow(0)
        else:
            self._segb_tree_model.removeRows(0, self._segb_tree_model.rowCount())

    def _on_segb_show_deleted_toggled(self, checked: bool) -> None:
        self._segb_show_deleted = checked
        self._segb_refresh_record_list()

    # ── Schema resolution ─────────────────────────────────────────────────────────

    def _segb_resolve_schema(self, ui_path: str) -> None:
        key = stream_key_for_path(ui_path)
        self._segb_stream_key = key
        schema, source = None, 'none'
        if key:
            user = self._segb_load_user_schema(key)
            if user:
                schema, source = user, 'user'
            elif get_schema(key):
                schema, source = get_schema(key), 'built-in'
        self._segb_schema = schema
        self._segb_schema_source = source
        self._update_segb_schema_label()

    def _segb_load_user_schema(self, key: str):
        case_dir = getattr(self, 'case_dir', None)
        if not case_dir:
            return None
        try:
            from db_utils import _open_results_db, load_segb_schema
            with closing(_open_results_db(case_dir)) as db:
                return load_segb_schema(db, key)
        except Exception:
            return None

    def _update_segb_schema_label(self) -> None:
        key = self._segb_stream_key or '—'
        self._segb_schema_label.setText(f"   Schema: {key} · {self._segb_schema_source}")

    # ── Record decoding / tree ─────────────────────────────────────────────────────

    def _on_segb_record_selected(self, row: int) -> None:
        self._segb_tree_model.removeRows(0, self._segb_tree_model.rowCount())
        if row < 0 or row >= len(self._segb_view):
            return
        data = self._segb_view[row][1].data
        root = self._segb_tree_model.invisibleRootItem()

        bbp = _bbp()
        if bbp is None:
            self._segb_add_row(root, '(payload)', 'bytes',
                               f'{len(data)} bytes — install blackboxprotobuf to decode')
            self._segb_add_row(root, '(hex)', '', _hex_preview(data))
            return

        schema = self._segb_schema or {}
        typedef = schema.get('typedef') or None
        self._segb_cur_labels = schema.get('labels') or {}
        self._segb_cur_hints = schema.get('hints') or {}
        self._segb_cur_raw = {}

        decoded = None
        if typedef:
            try:
                decoded, _ = bbp.decode_message(data, typedef)
            except Exception:
                decoded = None          # bad/edited typedef → fall back to auto
        if decoded is None:
            try:
                decoded, auto_td = bbp.decode_message(data)
            except Exception:
                self._segb_add_row(root, '(payload)', 'not protobuf', f'{len(data)} bytes')
                self._segb_add_row(root, '(hex)', '', _hex_preview(data))
                return
            self._segb_cur_raw = self._segb_raw_top_fields(bbp, data, auto_td)

        for k in _sorted_keys(decoded):
            self._segb_add_value(root, str(k), decoded[k], str(k))

    def _segb_add_row(self, parent, field: str, typ: str, value: str):
        fi, ti, vi = QStandardItem(field), QStandardItem(typ), QStandardItem(value)
        for it in (fi, ti, vi):
            it.setEditable(False)
        parent.appendRow([fi, ti, vi])
        return fi

    def _segb_add_value(self, parent, field, value, path):
        label = self._segb_cur_labels.get(path) if path else None
        hint = self._segb_cur_hints.get(path) if path else None
        disp = f'{field}  ·  {label}' if label else field

        if isinstance(value, dict):
            text = ''
            raw = self._segb_cur_raw.get(path)
            if raw is not None:
                preview = _printable(raw)
                if preview:
                    text = f'maybe text: "{preview}"'
            node = self._segb_add_row(parent, disp, 'message', text)
            for k in _sorted_keys(value):
                self._segb_add_value(node, str(k), value[k],
                                     f'{path}.{k}' if path else None)
        elif isinstance(value, list):
            node = self._segb_add_row(parent, disp, 'repeated', f'{len(value)} items')
            for i, v in enumerate(value):
                self._segb_add_value(node, f'[{i}]', v, None)
        elif isinstance(value, (bytes, bytearray)):
            b = bytes(value)
            if hint == 'text':
                self._segb_add_row(parent, disp, 'text', b.decode('utf-8', 'replace'))
                return
            preview = _printable(b)
            vtext = f'{len(b)} bytes' + (f'   ·   "{preview}"' if preview else '')
            node = self._segb_add_row(parent, disp, 'bytes', vtext)
            nested = self._segb_try_decode(b)
            if nested:
                child = self._segb_add_row(node, '(as protobuf)', 'message', '')
                for k in _sorted_keys(nested):
                    self._segb_add_value(child, str(k), nested[k], None)
            else:
                self._segb_add_row(node, '(hex)', '', _hex_preview(b))
        elif isinstance(value, bool):
            self._segb_add_row(parent, disp, 'bool', str(value))
        elif isinstance(value, (int, float)):
            typ = 'int' if isinstance(value, int) else 'double'
            if hint in ('cocoa', 'unix'):
                rendered = _render_epoch(hint, value)
                extra = f'   ⏱ {rendered}' if rendered else ''
            else:
                extra = _timestamp_hints(value)
            self._segb_add_row(parent, disp, typ, f'{value}{extra}')
        else:
            self._segb_add_row(parent, disp, type(value).__name__, str(value))

    def _segb_try_decode(self, b: bytes):
        if len(b) < 2:
            return None
        bbp = _bbp()
        if bbp is None:
            return None
        try:
            decoded, _ = bbp.decode_message(b)
            return decoded or None
        except Exception:
            return None

    def _segb_raw_top_fields(self, bbp, data: bytes, typedef) -> dict:
        if not isinstance(typedef, dict):
            return {}
        override = {}
        for k, td in typedef.items():
            if isinstance(td, dict) and td.get('type') in ('message', 'string', 'bytes'):
                override[k] = {'type': 'bytes'}
            else:
                override[k] = dict(td) if isinstance(td, dict) else td
        try:
            raw, _ = bbp.decode_message(data, override)
            return {k: v for k, v in raw.items() if isinstance(v, (bytes, bytearray))}
        except Exception:
            return {}

    # ── Schema editor ──────────────────────────────────────────────────────────────

    def _edit_segb_schema(self) -> None:
        if not self._segb_view:
            return
        bbp = _bbp()
        seed = self._segb_schema or {'typedef': {}, 'labels': {}, 'hints': {}}
        if not self._segb_schema and bbp is not None:
            row = min(max(self._segb_table_view.currentIndex().row(), 0),
                      len(self._segb_view) - 1)
            try:
                _, auto_td = bbp.decode_message(self._segb_view[row][1].data)
                seed = {'typedef': auto_td, 'labels': {}, 'hints': {}}
            except Exception:
                pass

        dlg = QDialog(self)
        dlg.setWindowTitle(f"SEGB schema — {self._segb_stream_key or 'unknown stream'}")
        dlg.resize(680, 560)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "Edit the schema as JSON: 'typedef' (blackboxprotobuf), "
            "'labels' {field.path: name}, 'hints' {field.path: cocoa|unix|text}."))
        editor = QPlainTextEdit()
        editor.setPlainText(json.dumps(seed, indent=2))
        v.addWidget(editor, stretch=1)
        err = QLabel("")
        err.setStyleSheet("color: #c62828;")
        v.addWidget(err)

        buttons = QDialogButtonBox()
        apply_btn = buttons.addButton("Apply", QDialogButtonBox.ButtonRole.ApplyRole)
        save_btn = buttons.addButton("Save to case", QDialogButtonBox.ButtonRole.AcceptRole)
        revert_btn = buttons.addButton("Revert to built-in", QDialogButtonBox.ButtonRole.ResetRole)
        buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        v.addWidget(buttons)

        has_case = bool(getattr(self, 'case_dir', None)) and bool(self._segb_stream_key)
        save_btn.setEnabled(has_case)
        if not has_case:
            save_btn.setToolTip("Open a case to save schemas persistently.")
        revert_btn.setEnabled(self._segb_schema_source == 'user')

        def parse_and_apply():
            try:
                schema = json.loads(editor.toPlainText())
                assert isinstance(schema, dict)
            except Exception as exc:
                err.setText(f"Invalid JSON: {exc}")
                return None
            schema.setdefault('typedef', {})
            schema.setdefault('labels', {})
            schema.setdefault('hints', {})
            td = schema['typedef'] or None
            if td and bbp is not None:
                try:
                    bbp.decode_message(self._segb_view[0][1].data, td)
                except Exception as exc:
                    err.setText(f"typedef does not decode the first record: {exc}")
                    return None
            err.setText("")
            self._segb_schema = schema
            self._segb_schema_source = 'edited'
            self._update_segb_schema_label()
            self._on_segb_record_selected(max(self._segb_table_view.currentIndex().row(), 0))
            return schema

        def on_apply():
            parse_and_apply()

        def on_save():
            schema = parse_and_apply()
            if schema is None:
                return
            try:
                from db_utils import _open_results_db, save_segb_schema
                with closing(_open_results_db(self.case_dir)) as db:
                    save_segb_schema(db, self._segb_stream_key, schema)
            except Exception as exc:
                err.setText(f"Save failed: {exc}")
                return
            self._segb_schema_source = 'user'
            self._update_segb_schema_label()
            dlg.accept()

        def on_revert():
            try:
                from db_utils import _open_results_db, delete_segb_schema
                with closing(_open_results_db(self.case_dir)) as db:
                    delete_segb_schema(db, self._segb_stream_key)
            except Exception as exc:
                err.setText(f"Revert failed: {exc}")
                return
            b = get_schema(self._segb_stream_key)
            self._segb_schema = b
            self._segb_schema_source = 'built-in' if b else 'none'
            self._update_segb_schema_label()
            self._on_segb_record_selected(max(self._segb_table_view.currentIndex().row(), 0))
            dlg.accept()

        apply_btn.clicked.connect(on_apply)
        save_btn.clicked.connect(on_save)
        revert_btn.clicked.connect(on_revert)
        buttons.rejected.connect(dlg.reject)
        dlg.exec()

    # ── Export ───────────────────────────────────────────────────────────────────

    def _export_segb_json(self) -> None:
        if not self._segb_view:
            return
        default = (self._segb_name or 'segb').rsplit('.', 1)[0] + '_records.json'
        path, _ = QFileDialog.getSaveFileName(
            self, "Export SEGB Records to JSON", default, "JSON Files (*.json)")
        if not path:
            return
        bbp = _bbp()
        typedef = (self._segb_schema or {}).get('typedef') or None
        out = []
        for i, rec in self._segb_view:
            entry = {
                'index': i,
                'timestamp': getattr(rec, 'timestamp1', None),
                'state': rec.state.name if isinstance(rec.state, EntryState) else str(rec.state),
                'size': len(rec.data),
                'crc_passed': bool(rec.crc_passed),
            }
            decoded = None
            if bbp is not None:
                if typedef:
                    try:
                        decoded, _ = bbp.decode_message(rec.data, typedef)
                    except Exception:
                        decoded = None
                if decoded is None:
                    try:
                        decoded, _ = bbp.decode_message(rec.data)
                    except Exception:
                        decoded = None
            if decoded is not None:
                entry['protobuf'] = decoded
            else:
                entry['raw_hex'] = rec.data.hex()
            out.append(entry)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(out, f, indent=2, default=_json_default, ensure_ascii=False)
        except Exception as exc:
            self._segb_status_label.setText(f"Export failed: {exc}")
            return
        self._segb_status_label.setText(f"Exported {len(out)} records to {path}")

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def _clear_segb_preview(self) -> None:
        self._segb_all = []
        self._segb_view = []
        self._segb_show_deleted = False
        self._segb_stream_key = None
        self._segb_schema = None
        self._segb_schema_source = 'none'
        if hasattr(self, '_segb_record_model'):
            self._segb_record_model.removeRows(0, self._segb_record_model.rowCount())
        if hasattr(self, '_segb_tree_model'):
            self._segb_tree_model.removeRows(0, self._segb_tree_model.rowCount())
        if hasattr(self, '_segb_deleted_check'):
            self._segb_deleted_check.blockSignals(True)
            self._segb_deleted_check.setChecked(False)
            self._segb_deleted_check.setVisible(False)
            self._segb_deleted_check.blockSignals(False)
        if hasattr(self, '_segb_export_btn'):
            self._segb_export_btn.setEnabled(False)
        if hasattr(self, '_segb_edit_btn'):
            self._segb_edit_btn.setEnabled(False)
        if hasattr(self, '_segb_schema_label'):
            self._segb_schema_label.setText("   Schema: —")
        if hasattr(self, '_segb_status_label'):
            self._segb_status_label.setText("No SEGB file selected")
