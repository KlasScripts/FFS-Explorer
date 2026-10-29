"""File Preview database view: before and after the WAL, and WAL history."""
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _database_with_wal(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.executescript("""
        CREATE TABLE msg (_id INTEGER PRIMARY KEY, text TEXT);
        INSERT INTO msg VALUES (1,'keep'),(2,'change me'),(3,'delete me'),(4,'edit twice');
        CREATE TABLE gone (_id INTEGER PRIMARY KEY, x TEXT);
        INSERT INTO gone VALUES (1,'dropped');
    """)
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for sql in (
        "UPDATE msg SET text='changed' WHERE _id=2",
        "DELETE FROM msg WHERE _id=3",
        "INSERT INTO msg VALUES (10,'new')",
        "INSERT INTO msg VALUES (11,'added then deleted')",
        "DELETE FROM msg WHERE _id=11",
        "UPDATE msg SET text='first edit' WHERE _id=4",
        "UPDATE msg SET text='second edit' WHERE _id=4",
        "DROP TABLE gone",
    ):
        con.execute(sql)
        con.commit()
    files = [open(str(db) + ext, "rb").read() for ext in ("", "-wal", "-shm")]
    con.close()
    return files


def _viewer(cls, result):
    v = cls()
    v._sql_before_path = result["before_path"]
    v._sql_db_path = result["db_path"]
    v._sql_wal_copy = result["wal_copy"]
    v._sql_main_copy = result["main_copy"]
    v._sql_journal_copy = result["journal_copy"]
    v._sql_dropped = set(result["dropped"])
    v._sql_has_wal = result["has_wal"]
    v._sql_meta_conn = sqlite3.connect(result["db_path"])
    return v


def _load(raw, journal=None, wal=None, shm=None):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from sqlite_viewer import SqlitePreviewWorker, SqliteViewerMixin

    class Viewer(SqliteViewerMixin):
        pass

    result = {}
    worker = SqlitePreviewWorker(raw, "t.db", wal, shm, journal)
    worker.done.connect(result.update)
    worker.run()
    return _viewer(Viewer, result), result


def test_deleted_row_in_free_space_is_carved(tmp_path):
    db = tmp_path / "plain.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA secure_delete=OFF")
    con.execute("CREATE TABLE note (_id INTEGER PRIMARY KEY, kind INTEGER, text TEXT)")
    # Rowids above 127 and text over 127 bytes: the 4 bytes a delete
    # overwrites are then only the length and rowid, as in real app rows.
    lines = [(200 + i, 1, f"note number {i} " + "x" * 140) for i in range(8)]
    con.executemany("INSERT INTO note VALUES (?,?,?)", lines)
    con.commit()
    con.execute("DELETE FROM note WHERE _id=203")
    con.commit()
    con.close()
    v, result = _load(open(db, "rb").read())
    try:
        assert not result["has_wal"]
        _cols, rows, n = v._compute_table_diff("note")
        assert n["carved"] == 1 and n["deleted"] == 0
        # An ordinary delete loses the rowid, so the row is found by its
        # record pattern, and says so.
        carved = [r for s, r, _ in rows if s == "CARVED (pattern)"]
        assert carved[0][1:] == [1, lines[3][2]]
        assert carved[0][0] is None          # no rowid survives an ordinary delete
    finally:
        v._sql_meta_conn.close()
        shutil.rmtree(result["tmpdir"], ignore_errors=True)


def test_rollback_journal_page_is_an_older_version(tmp_path):
    import struct
    db = tmp_path / "j.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.executescript("""
        CREATE TABLE note (_id INTEGER PRIMARY KEY, text TEXT);
        INSERT INTO note VALUES (1,'before the change');
    """)
    con.commit()
    page_size = con.execute("PRAGMA page_size").fetchone()[0]
    root = con.execute("SELECT rootpage FROM sqlite_master WHERE name='note'").fetchone()[0]
    old_page = open(db, "rb").read()[(root - 1) * page_size:root * page_size]
    con.execute("UPDATE note SET text='after the change' WHERE _id=1")
    con.commit()
    con.close()
    sector = 512
    header = (b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
              + struct.pack(">IIIII", 1, 0, root, sector, page_size)).ljust(sector, b"\0")
    journal = header + struct.pack(">I", root) + old_page + b"\0\0\0\0"
    v, result = _load(open(db, "rb").read(), journal=journal)
    try:
        assert result["journal_copy"]
        _cols, rows, n = v._compute_table_diff("note")
        shown = [(s, r[0], r[1]) for s, r, _ in rows]
        assert ("", 1, "after the change") in shown
        assert ("JOURNAL", 1, "before the change") in shown and n["journal"] == 1
    finally:
        v._sql_meta_conn.close()
        shutil.rmtree(result["tmpdir"], ignore_errors=True)


def test_wal_view_keeps_before_after_and_history(tmp_path):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from sqlite_viewer import SqlitePreviewWorker, SqliteViewerMixin

    raw, wal, shm = _database_with_wal(tmp_path)
    result = {}
    worker = SqlitePreviewWorker(raw, "t.db", wal, shm)
    worker.done.connect(result.update)
    worker.run()
    try:
        assert result["has_wal"] and result["dropped"] == ["gone"]
        assert "gone" in result["tables"]

        class Viewer(SqliteViewerMixin):
            pass

        v = _viewer(Viewer, result)

        _cols, rows, n = v._compute_table_diff("msg")
        assert (n["before"], n["deleted"], n["modified"], n["added"], n["history"]) == (4, 1, 2, 1, 2)
        shown = [(status, vals[0], vals[1]) for status, vals, _ in rows]
        assert ("", 1, "keep") in shown
        assert ("MODIFIED", 2, "change me → changed") in shown
        assert ("DELETED", 3, "delete me") in shown
        assert ("MODIFIED", 4, "edit twice → second edit") in shown
        assert ("ADDED", 10, "new") in shown
        assert ("WAL HISTORY", 11, "added then deleted") in shown
        assert ("WAL HISTORY", 4, "first edit") in shown

        from PySide6.QtCore import Qt
        from sqlite_viewer import SqliteDiffModel
        model = SqliteDiffModel()
        model.load(_cols, rows)
        tips = {model.data(model.index(i, 0)): model.data(model.index(i, 0), Qt.ItemDataRole.ToolTipRole)
                for i in range(model.rowCount())}
        assert tips["WAL HISTORY"].startswith("Older version in a WAL frame")
        import sqlite_viewer
        assert "Table not certain" in sqlite_viewer._DIFF_TIP["CARVED (freed page)"]
        assert tips[""] is None
        assert all(len(t.split()) <= 10 for t in tips.values() if t)
        assert model.data(model.index(0, 1), Qt.ItemDataRole.ToolTipRole) is None

        model.sort(2, Qt.SortOrder.AscendingOrder)       # the text column
        texts = [model.data(model.index(i, 2)) for i in range(model.rowCount())]
        assert texts[0] == "added then deleted"            # "added…" sorts before "change…"
        model.sort(1, Qt.SortOrder.DescendingOrder)      # the id column, as numbers
        ids = [model.data(model.index(i, 1)) for i in range(model.rowCount())]
        assert ids[0] == "11" and ids[-1] == "1"

        _cols, rows, n = v._compute_table_diff("gone")
        assert (n["before"], n["deleted"]) == (1, 1) and rows[0][0] == "DELETED"
        v._sql_meta_conn.close()
        # The pre-WAL copy is untouched by the view.
        assert open(result["before_path"], "rb").read() == raw
    finally:
        shutil.rmtree(result["tmpdir"], ignore_errors=True)


def test_export_keeps_the_stored_number_beside_a_converted_time(tmp_path, monkeypatch):
    """A guessed date column is exported twice: the stored number under the
    column's own name, then the converted text. A wrong guess can then never
    replace the stored value in a CSV."""
    import csv
    from PySide6.QtWidgets import QApplication, QFileDialog
    QApplication.instance() or QApplication([])
    from sqlite_viewer import SqliteViewerMixin, SqlitePreviewWorker
    from PySide6.QtWidgets import QWidget

    class Viewer(QWidget, SqliteViewerMixin):
        pass

    db = tmp_path / "times.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ev (_id INTEGER PRIMARY KEY, t INTEGER, note TEXT)")
    con.executemany("INSERT INTO ev VALUES (?,?,?)",
                    [(1, 1793265311042, "a"), (2, 1793265400000, "b")])
    con.commit()
    con.close()
    v = Viewer()
    tab = v._setup_sqlite_tab()          # keep the widgets alive
    v._sql_ui_path = "data/data/example/databases/times.db"
    result = {}
    worker = SqlitePreviewWorker(open(db, "rb").read(), "times.db", None, None, None)
    worker.done.connect(result.update)
    worker.run()
    v._on_sqlite_preview_ready(result)
    out = tmp_path / "out.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    v._export_sql_table_csv()
    rows = list(csv.reader(open(out, encoding="utf-8")))
    header = rows[0]
    assert "t" in header
    i = header.index("t")
    assert "converted" in header[i + 1]
    assert rows[1][i] == "1793265311042"
    assert rows[1][i + 1] != rows[1][i]
    v._clear_sqlite_preview()


def test_pattern_carved_row_marks_missing_columns(tmp_path):
    from sqlite_viewer import _CUT_OFF
    assert _CUT_OFF == "[cut off]"
