"""Media Browser tiles say why there is no thumbnail (2026-11-01): the
ThumbnailWorker reports a reason for each file it can't make one for, instead
of skipping it silently (the tile used to stay blank for good, looking like
one still loading). Run on a real stored zip with real and broken files."""
import io
import os
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def mv():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import media_viewer
    return media_viewer


def _jpeg():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (0, 128, 255)).save(buf, format="JPEG")
    return buf.getvalue()


def test_worker_reports_why(mv, tmp_path):
    files = {
        "ok.jpg": _jpeg(),
        "broken.jpg": b"\xff\xd8\xff\xe0" + os.urandom(2000),   # JPEG start, then junk
        "empty.jpg": b"",
        "notes.dat": b"just some text, not a picture",
    }
    zpath = str(tmp_path / "ffs.zip")
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    with zipfile.ZipFile(zpath) as z:
        infos = {i.filename: i for i in z.infolist()}

    class View:                      # the CachedZipView calls the worker makes
        def getinfo(self, name):
            return infos[name]
    worker = mv.ThumbnailWorker(zpath, list(files) + ["missing.jpg"], lambda p: p, 96,
                                {n: i.file_size for n, i in infos.items()})
    got, failed = {}, {}
    worker.thumbnail_ready.connect(lambda p, img: got.__setitem__(p, img))
    worker.thumbnail_failed.connect(lambda p, why: failed.__setitem__(p, why))
    view = View()
    worker._locate = (lambda orig: (lambda p, _v: orig(p, view) if p in infos else None))(worker._locate)
    worker.run()

    W = mv.ThumbnailWorker
    assert set(got) == {"ok.jpg"} and not got["ok.jpg"].isNull()
    assert failed == {"broken.jpg": W.WHY_UNDECODABLE, "empty.jpg": W.WHY_EMPTY,
                      "notes.dat": W.WHY_NOT_MEDIA, "missing.jpg": W.WHY_UNREADABLE}


def test_delegate_shows_reason_and_stops_refetching(mv):
    d = mv.MediaGridDelegate(96)
    assert not d.has_result("a.jpg")
    d.set_failed("a.jpg", mv.ThumbnailWorker.WHY_UNDECODABLE)
    assert d.has_result("a.jpg") and not d.has_pixmap("a.jpg")
    d.clear()
    assert not d.has_result("a.jpg")
