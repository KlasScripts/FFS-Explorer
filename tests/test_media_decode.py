"""Media Browser decoding (ASSUMPTIONS.md A-64): orientation is applied once,
the same way for thumbnails and the full-size view, and the thumbnail cache
drops thumbnails made before the orientation fixes exactly once per case.

Images are synthetic: one half red, the other blue, tagged Orientation=6
("turn 90 degrees clockwise to show"), so the right result is portrait with
the red half on top. Measured on real data (IOS17): 139/139 HEIC photos and
14/14 videos match iOS's own PhotoData thumbnails; video rotation has no
synthetic test here (PyAV can't easily write a display matrix)."""
import io
import os
import sqlite3

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def mv():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import media_viewer
    return media_viewer


def _half_red_half_blue(w, h):
    from PIL import Image
    img = Image.new("RGB", (w, h), (255, 0, 0))
    img.paste((0, 0, 255), (w // 2, 0, w, h))
    return img


def _top_is_red(qimg):
    from PySide6.QtGui import QColor
    return QColor(qimg.pixel(qimg.width() // 2, 1)).red() > 128


def _encode(fmt, w, h, orientation):
    img = _half_red_half_blue(w, h)
    exif = img.getexif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format=fmt, exif=exif.tobytes(), quality=95)
    return buf.getvalue()


def test_heic_orientation_is_applied_once(mv):
    """pillow_heif already rotates while decoding; rotating again (the old
    _load_qimage fix) showed 73 of 137 IOS17 HEIC photos sideways."""
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    data = _encode("HEIF", 64, 32, 6)
    img = mv._heif_to_qimage(data)
    assert (img.width(), img.height()) == (32, 64)
    assert _top_is_red(img)
    small = mv._heif_to_qimage(data, max_side=16)
    assert (small.width(), small.height()) == (8, 16)
    assert _top_is_red(small)


def test_jpeg_thumbnail_matches_full_decode_orientation(mv):
    """The thumbnail asks the decoder for a reduced size; EXIF orientation
    must still be applied exactly as in the full-size decode."""
    data = _encode("JPEG", 1280, 640, 6)
    full = mv._load_qimage(data, ".jpg")
    thumb = mv._image_thumbnail(data, ".jpg", 160)
    assert (full.width(), full.height()) == (640, 1280)
    assert (thumb.width(), thumb.height()) == (80, 160)
    assert _top_is_red(full) and _top_is_red(thumb)


def test_small_image_is_not_enlarged_by_the_reader(mv):
    data = _encode("PNG", 100, 50, 1)
    thumb = mv._image_thumbnail(data, ".png", 160)
    assert (thumb.width(), thumb.height()) == (160, 80)   # scaled to the box, like before


def test_outdated_thumbnails_are_purged_once_per_case(mv, tmp_path):
    from contextlib import closing
    from db_utils import _open_cache_db
    with closing(_open_cache_db(str(tmp_path))) as db:
        db.execute("INSERT INTO thumbnails VALUES ('a.heic', 1, 160, x'00')")
        db.commit()
        mv._thumb_cache_checked.clear()
        mv._purge_outdated_thumbnails(db)
        assert db.execute("SELECT COUNT(*) FROM thumbnails").fetchone()[0] == 0
        marker = db.execute(
            "SELECT logic_version FROM processing_registry "
            "WHERE ui_path='' AND capability='thumbnail_cache'").fetchone()
        assert marker == (mv._THUMB_CACHE_LOGIC,)
        # a thumbnail made by the current decoder survives every later check
        db.execute("INSERT INTO thumbnails VALUES ('b.heic', 1, 160, x'00')")
        db.commit()
        mv._purge_outdated_thumbnails(db)
        mv._thumb_cache_checked.clear()          # as in a fresh app session
        mv._purge_outdated_thumbnails(db)
        assert db.execute("SELECT COUNT(*) FROM thumbnails").fetchone()[0] == 1
