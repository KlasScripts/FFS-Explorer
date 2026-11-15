"""Full-size viewer row mode and the hex panel that follows it.

A report row can hold more than one picture (Likely Full-screen Views: a
leftover and a scan). The examiner must always be able to tell which one
the viewer and the hex panel show, and the hex panel must never move to a
picture the examiner did not choose without saying so.
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication, QLabel, QPushButton


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def _png(w, h, colour):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(colour))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, 'PNG')
    return bytes(ba)


def _items(n=2):
    out = [('thumbnail', 'cache/a.0', _png(40, 30, 'red'))]
    if n > 1:
        out.append(('paired_scan_thumb', 'cache/b.0', _png(20, 15, 'blue')))
    return out


def _captions(dialog):
    return [l.text() for l in dialog.findChildren(QLabel)
            if l.text().startswith(('Showing', '<b>'))]


def _press(dialog, label):
    next(b for b in dialog.findChildren(QPushButton) if b.text() == label).click()


# --- the dialog -------------------------------------------------------------

def test_row_mode_names_the_picture_shown(app):
    from media_viewer import MediaFullViewDialog
    items = _items()
    d = MediaFullViewDialog('cache/a.0', items[0][2])
    d.load_row(items, 'paired_scan_thumb')
    assert d.current_mode() == 'paired_scan_thumb'
    assert d.windowTitle().startswith('paired_scan_thumb')
    caps = _captions(d)
    assert len(caps) == 1 and 'paired_scan_thumb' in caps[0] and '20×15 px' in caps[0]
    buttons = {b.text(): b.isChecked() for b in d.findChildren(QPushButton) if b.isCheckable()}
    assert buttons == {'thumbnail': False, 'paired_scan_thumb': True,
                       MediaFullViewDialog.SIDE_BY_SIDE: False}


def test_side_by_side_captions_both(app):
    from media_viewer import MediaFullViewDialog
    items = _items()
    d = MediaFullViewDialog('cache/a.0', items[0][2])
    d.load_row(items, MediaFullViewDialog.SIDE_BY_SIDE)
    caps = _captions(d)
    assert len(caps) == 2
    assert 'thumbnail' in caps[0] and '40×30 px' in caps[0]
    assert 'paired_scan_thumb' in caps[1] and '20×15 px' in caps[1]


def test_missing_column_falls_back_and_says_so(app):
    from media_viewer import MediaFullViewDialog
    items = _items()
    d = MediaFullViewDialog('cache/a.0', items[0][2])
    d.load_row(items, 'file_link')
    assert d.current_mode() == 'thumbnail'
    assert '(no file_link on this row)' in _captions(d)[0]


def test_side_by_side_on_a_one_picture_row(app):
    """Used to raise StopIteration (mode became None)."""
    from media_viewer import MediaFullViewDialog
    items = _items(1)
    d = MediaFullViewDialog('cache/a.0', items[0][2])
    d.load_row(items, MediaFullViewDialog.SIDE_BY_SIDE)
    assert d.current_mode() == 'thumbnail'
    assert '(only one picture on this row)' in _captions(d)[0]


def test_bar_click_emits_the_clicked_mode(app):
    from media_viewer import MediaFullViewDialog
    items = _items()
    d = MediaFullViewDialog('cache/a.0', items[0][2])
    d.load_row(items, 'thumbnail')
    got = []
    d.mode_chosen.connect(got.append)
    _press(d, 'paired_scan_thumb')
    _press(d, MediaFullViewDialog.SIDE_BY_SIDE)
    assert got == ['paired_scan_thumb', MediaFullViewDialog.SIDE_BY_SIDE]
    # A caller's own load_row (row change) is not an examiner choice.
    d.load_row(items, 'thumbnail')
    assert got == ['paired_scan_thumb', MediaFullViewDialog.SIDE_BY_SIDE]


# --- the hex panel follows the examiner's choice ---------------------------

class _Mod:
    media_fields = ['thumbnail', 'paired_scan_thumb']
    media_compare = True


def _rows():
    """Three Likely-style rows; the last has no scan."""
    return [
        {'thumbnail': 'cache/r0a', 'paired_scan_thumb': 'cache/r0b'},
        {'thumbnail': 'cache/r1a', 'paired_scan_thumb': 'cache/r1b'},
        {'thumbnail': 'cache/r2a', 'paired_scan_thumb': ''},
    ]


class _Model:
    def __init__(self, rows):
        self.rows = rows
        self.cols = ['path', 'thumbnail', 'paired_scan_thumb']

    def row_dict(self, r):
        return self.rows[r]

    def column_name(self, c):
        return self.cols[c]

    def media_columns(self):
        return {1, 2}

    def data(self, index):
        return self.rows[index.row()][self.cols[index.column()]]


class _Index:
    def __init__(self, r, c=0):
        self._r, self._c = r, c

    def isValid(self):
        return True

    def row(self):
        return self._r

    def column(self):
        return self._c


class _View:
    def __init__(self):
        self.idx = _Index(0)

    def currentIndex(self):
        return self.idx


class _Status:
    def __init__(self):
        self.msg = ''

    def showMessage(self, m, *_a):
        self.msg = m


class _Btn:
    def setChecked(self, _v):
        pass


def _host(app):
    from artifact_viewer import ArtifactViewerMixin
    from PySide6.QtWidgets import QWidget

    class Host(QWidget, ArtifactViewerMixin):
        pass

    h = Host()          # a QWidget: the viewer is parented to it
    blobs = {p: _png(10 + i, 10, 'green') for i, p in enumerate(
        v for r in _rows() for v in r.values() if v)}
    h._art_current_mod = _Mod()
    h._art_media_dialog = None
    h._art_hex_media_field = None
    h._art_chat_summary = None
    h._art_table_model = _Model(_rows())
    h._art_report_view = _View()
    h.status_bar = _Status()
    h._hex_source_attach_btn = _Btn()
    h.hex_loaded = []
    h._read_zip_bytes = lambda p: blobs.get(p)
    h._hex_source_is_record = lambda: False
    h._load_hex_preview_from_bytes = lambda data, p, *a, **k: h.hex_loaded.append(p)
    h._show_art_hex_message = lambda m: h.hex_loaded.append(('message', m))
    h._on_art_media_dialog_closed = lambda *_a: None
    return h


def _select(h, r):
    h._art_report_view.idx = _Index(r)
    row = h._art_table_model.row_dict(r)
    h._art_load_attachment_hex(row)
    h._art_sync_open_media_dialog(row)


def test_hex_follows_choice_across_rows_side_by_side(app):
    from media_viewer import MediaFullViewDialog
    h = _host(app)
    # Double-click the scan on row 0: Likely opens side by side, hex = scan.
    h._on_art_report_double_clicked(_Index(0, 2))
    d = h._art_media_dialog
    d.show()        # isVisible() is false offscreen until shown (A-62)
    assert d.current_mode() == MediaFullViewDialog.SIDE_BY_SIDE
    assert h.hex_loaded[-1] == 'cache/r0b'
    assert h.status_bar.msg.startswith('Hex: paired_scan_thumb')
    # Move to row 1 while side by side: hex stays on the scan.
    _select(h, 1)
    assert d.current_mode() == MediaFullViewDialog.SIDE_BY_SIDE
    assert h.hex_loaded[-1] == 'cache/r1b'
    # Row 2 has no scan: hex falls back to the thumbnail and says so.
    _select(h, 2)
    assert h.hex_loaded[-1] == 'cache/r2a'
    assert '(no paired_scan_thumb on this row)' in h.status_bar.msg
    # Back to row 1: the choice was not lost by the fallback.
    _select(h, 1)
    assert h.hex_loaded[-1] == 'cache/r1b'
    d.close()


def test_bar_click_moves_hex_at_once(app):
    from media_viewer import MediaFullViewDialog
    h = _host(app)
    h._on_art_report_double_clicked(_Index(0, 2))
    d = h._art_media_dialog
    d.show()
    _press(d, 'thumbnail')
    assert d.current_mode() == 'thumbnail'
    assert h.hex_loaded[-1] == 'cache/r0a'
    assert h.status_bar.msg.startswith('Hex: thumbnail')
    # Side by side keeps the hex panel on the last chosen picture.
    _press(d, MediaFullViewDialog.SIDE_BY_SIDE)
    assert h.hex_loaded[-1] == 'cache/r0a'
    _select(h, 1)
    assert h.hex_loaded[-1] == 'cache/r1a'
    d.close()


def test_choice_survives_a_row_that_lacks_it(app):
    """A fallback on one row must not become the choice for the next."""
    from media_viewer import MediaFullViewDialog
    two, one = _items(), _items(1)
    d = MediaFullViewDialog('cache/a.0', two[0][2])
    d.load_row(two, 'paired_scan_thumb')
    d.load_row(one, d.wanted_mode())
    assert d.current_mode() == 'thumbnail'
    assert d.wanted_mode() == 'paired_scan_thumb'
    d.load_row(two, d.wanted_mode())
    assert d.current_mode() == 'paired_scan_thumb'
    d.load_row(two, MediaFullViewDialog.SIDE_BY_SIDE)
    d.load_row(one, d.wanted_mode())
    d.load_row(two, d.wanted_mode())
    assert d.current_mode() == MediaFullViewDialog.SIDE_BY_SIDE
