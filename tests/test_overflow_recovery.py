"""Deleted media bigger than one SQLite page (sqlite_carve.carve_overflowing_records,
embedded_media_scan.scan_sqlite_overflow). Databases are written by the real
SQLite engine with real JPEG images (Pillow, random noise so they don't
compress), then known rows are deleted. Ground truth is the original bytes:
a 'complete' result must be byte-identical to a deleted original, a partial
must be a true prefix of one, and nothing may be recovered under
secure_delete."""
import hashlib
import io
import os
import random
import sqlite3
import tempfile

import pytest

PIL = pytest.importorskip('PIL.Image')

import embedded_media_scan as ems
import sqlite_carve as sc

DELETE = [2, 5, 9, 11, 14, 17]


def _images(n=20, seed=7):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        side = rnd.choice([60, 90, 130, 180])
        im = PIL.frombytes('RGB', (side, side), bytes(rnd.getrandbits(8) for _ in range(side * side * 3)))
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=90)
        out.append(buf.getvalue())
    return out


def _db(path, imgs, wal=False, secure=False, checkpoint_first=True):
    c = sqlite3.connect(path, isolation_level=None)
    c.execute('pragma page_size=4096')
    if wal:
        c.execute('pragma journal_mode=wal')
        c.execute('pragma wal_autocheckpoint=0')
    if secure:
        c.execute('pragma secure_delete=on')
    c.execute('create table media(id integer primary key, name text, data blob)')
    if wal and checkpoint_first:
        c.execute('pragma wal_checkpoint(TRUNCATE)')
    for i, b in enumerate(imgs):
        c.execute('insert into media(name, data) values (?, ?)', (f'img{i}', b))
    for rowid in DELETE:
        c.execute('delete from media where id = ?', (rowid,))
    return c


def _check(raw, wal, imgs):
    deleted = {hashlib.sha256(imgs[r - 1]).hexdigest() for r in DELETE}
    every = {hashlib.sha256(b).hexdigest() for b in imgs}
    exact = set()
    for rec in sc.carve_overflowing_records(raw, wal):
        for _, blob, size in sc.blob_columns(rec['payload']):
            if rec['status'] == 'complete' and len(blob) == size:
                assert hashlib.sha256(blob).hexdigest() in every   # never wrong bytes
                exact.add(hashlib.sha256(blob).hexdigest())
            elif len(blob) >= 256 and ems.classify_media_blob(blob):
                assert any(o.startswith(blob) for o in imgs)        # a true prefix
    return exact & deleted


def test_images_are_bigger_than_a_page():
    assert all(len(b) > 4096 for b in _images())


def test_rollback_delete_never_wrong(tmp_path):
    imgs = _images()
    c = _db(str(tmp_path / 'a.db'), imgs)
    c.close()
    _check(open(tmp_path / 'a.db', 'rb').read(), None, imgs)


def test_everything_in_wal_recovers_every_deleted_image(tmp_path):
    imgs = _images()
    db = str(tmp_path / 'w.db')
    c = _db(db, imgs, wal=True)
    raw, wal = open(db, 'rb').read(), open(db + '-wal', 'rb').read()
    c.close()
    assert len(_check(raw, wal, imgs)) == len(DELETE)


def test_secure_delete_recovers_nothing(tmp_path):
    imgs = _images()
    c = _db(str(tmp_path / 's.db'), imgs, secure=True)
    c.close()
    assert not _check(open(tmp_path / 's.db', 'rb').read(), None, imgs)


def test_sweep_names_and_wal_aware_live_scan(tmp_path):
    imgs = _images()
    db = str(tmp_path / 'w.db')
    c = _db(db, imgs, wal=True)
    raw, wal = open(db, 'rb').read(), open(db + '-wal', 'rb').read()
    c.close()
    rows = ems.scan_container('sqlite', 'x/w.db', raw, tempfile.mkdtemp(), 'test', wal_bytes=wal)
    live = [r for r in rows if not r['recovered']]
    assert len(live) == len(imgs) - len(DELETE)        # rows only in the -wal count as live
    deleted = {hashlib.sha256(imgs[r - 1]).hexdigest() for r in DELETE}
    carved = {r['sha256'] for r in rows if 'overflow chain' in (r['recovery_source'] or '')
              and 'PARTIAL' not in r['recovery_source']}
    assert carved == deleted                           # no duplicates of live media
    names = {r['display_name'] for r in rows if r['recovered']}
    assert any(n.startswith('w.db-carved') for n in names)
    assert all('-carved-partial' in r['display_name'] for r in rows if 'PARTIAL' in (r['recovery_source'] or ''))
