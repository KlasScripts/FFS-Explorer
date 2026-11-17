"""File events on the Timeline (timeline.file_events): User Media files'
own times (never Accessed) and their taken time when it has an offset;
proven copies with the same time are one event."""
import sqlite3

import timeline

COLS = [('Created', 'btime'), ('Changed', 'ctime'), ('Modified', 'mtime'), ('Accessed', 'atime')]
DONE = {'media_metadata': (True, 2), 'duplicate_detection': (True, None)}


def _ev(events):
    return {(e[3], e[13]): e for e in events}


def test_times_a_file_shares_are_one_event():
    meta = {'DCIM/a.jpg': {'btime': 1700000000, 'ctime': 1700000000, 'mtime': 1700000000,
                           'atime': 1700000000}}
    events, _ = timeline.file_events(['DCIM/a.jpg'], meta, COLS, {}, {}, runs=DONE)
    assert len(events) == 1
    e = events[0]
    assert e[3] == 'btime+ctime+mtime'
    assert e[6] == 'Created, Changed and Modified (file system)'
    assert 'Created:' in e[12] and 'Modified:' in e[12]
    meta['DCIM/a.jpg']['mtime'] = 1700000500
    events, _ = timeline.file_events(['DCIM/a.jpg'], meta, COLS, {}, {}, runs=DONE)
    assert sorted(e[6] for e in events) == ['Created and Changed (file system)',
                                            'Modified (file system)']


def test_file_times_without_accessed():
    meta = {'DCIM/a.jpg': {'btime': 1700000000, 'ctime': 1700000100,
                           'mtime': 1700000200, 'atime': 1700000300}}
    events, sources = timeline.file_events(['DCIM/a.jpg'], meta, COLS, {}, {}, runs=DONE)
    assert sorted(e[3] for e in events) == ['btime', 'ctime', 'mtime']
    e = _ev(events)[('mtime', 'DCIM/a.jpg')]
    assert e[0] == 1700000200000 and e[1] == timeline.FILES_SCRIPT
    assert e[6] == 'Modified (file system)' and e[9] == 'a.jpg' and e[12]   # caution
    left = [s for s in sources if s[2] == 'Accessed']
    assert left and left[0][3] == timeline.NOT_AN_EVENT


def test_nanosecond_metadata_and_blank_times():
    meta = {'a.jpg': {'mtime': 1700000200 * 10**9, 'ctime': 0}}
    events, sources = timeline.file_events(['a.jpg'], meta, COLS[1:3], {}, {}, runs=DONE)
    assert [(e[3], e[0]) for e in events] == [('mtime', 1700000200000)]
    changed = next(s for s in sources if s[2] == 'Changed')
    assert changed[3] == timeline.NO_READABLE and changed[5] == 1      # one blank


def test_taken_only_with_an_offset():
    exif = {'a.jpg': {'taken': '2019-06-06 12:00:00', 'taken_offset': '+02:00'},
            'b.jpg': {'taken': '2019-06-06 12:00:00'},                       # local only
            'c.mp4': {'taken': '2019-06-06 10:00:00', 'taken_offset': '+00:00',
                      'duration_s': 3.0}}
    events, sources = timeline.file_events(list(exif), {}, [], exif, {}, runs=DONE)
    got = {e[13]: e for e in events}
    assert set(got) == {'a.jpg', 'c.mp4'}
    assert got['a.jpg'][0] == 1559815200000 == got['c.mp4'][0]
    assert 'offset +02:00' in got['a.jpg'][6]
    assert got['c.mp4'][6] == 'Recorded (video container time, UTC)'
    row = next(s for s in sources if s[2] == 'Taken (EXIF / video)')
    assert row[4] == 2 and '1 photos' in row[9] and 'without a UTC offset' in row[9]


def test_old_exif_run_is_not_used():
    exif = {'a.jpg': {'taken': '2019-06-06 12:00:00', 'taken_offset': '+02:00'}}
    for runs in ({}, {'media_metadata': (True, None)}, {'media_metadata': (False, 2)}):
        events, sources = timeline.file_events(['a.jpg'], {}, [], exif, {}, runs=runs)
        row = next(s for s in sources if s[2] == 'Taken (EXIF / video)')
        # events are still built from what is stored, but the Sources row says
        # the stage must be run again
        assert row[3] == timeline.NOT_RUN and 'Process Case' in row[9]


def test_proven_copies_with_the_same_time_are_one_event():
    meta = {'data/media/0/DCIM/a.jpg': {'mtime': 1700000200},
            'data/media/0/Pictures/WhatsApp/a.jpg': {'mtime': 1700000200},
            'data/media/0/Download/a.jpg': {'mtime': 1700009999},   # same bytes, other time
            'data/media/0/DCIM/b.jpg': {'mtime': 1700000200}}         # other file, same time
    md5 = {'data/media/0/DCIM/a.jpg': 'X', 'data/media/0/Pictures/WhatsApp/a.jpg': 'X',
           'data/media/0/Download/a.jpg': 'X'}
    events, _ = timeline.file_events(list(meta), meta, [('Modified', 'mtime')], {}, md5,
                                     runs=DONE)
    by = sorted((e[0], e[13], e[10]) for e in events)
    assert len(by) == 3
    same = [b for b in by if b[1] == 'data/media/0/DCIM/a.jpg'][0]
    assert '+1 identical copy with this time' in same[2]
    assert any(b[1] == 'data/media/0/Download/a.jpg' for b in by)     # differing time kept
    assert any(b[1] == 'data/media/0/DCIM/b.jpg' for b in by)         # not merged by time alone


def test_size_crc_alone_does_not_merge():
    meta = {'a/x.jpg': {'mtime': 1700000200}, 'b/x.jpg': {'mtime': 1700000200}}
    events, sources = timeline.file_events(list(meta), meta, [('Modified', 'mtime')], {}, {},
                                           runs={'media_metadata': (True, 2)})
    assert len(events) == 2
    assert 'Duplicate detection not run' in next(s for s in sources if s[2] == 'Modified')[9]


def test_build_with_files_and_path_column(tmp_path):
    res = sqlite3.connect(':memory:')
    res.execute('CREATE TABLE run_log (id INTEGER PRIMARY KEY, run_type TEXT, complete INTEGER,'
                ' parser_version INTEGER)')
    cache = sqlite3.connect(':memory:')
    meta = {'DCIM/a.jpg': {'mtime': 1700000200}}
    files = {'key': 'k', 'collect': lambda: (['DCIM/a.jpg'], meta, [('Modified', 'mtime')], 3)}
    out = timeline.build(res, cache, {}, files=files)
    assert out['events'] == 1
    rows, total = timeline.query(cache, text='DCIM/a')
    assert total == 1 and rows[0][timeline.COLUMNS.index('path')] == 'DCIM/a.jpg'
    note = next(s for s in timeline.source_list(cache) if s[2] == 'Modified')[9]
    assert '3 files on the User Media ignore list' in note
    # the file key is part of the fingerprint
    assert timeline.fingerprint(res, {}, 'k') != timeline.fingerprint(res, {}, 'k2')
    assert timeline.fingerprint(res, {}, 'k') == timeline.stored_fingerprint(cache)


def test_cache_media_and_archive_sources():
    import sqlite3
    res = sqlite3.connect(':memory:')
    res.execute('CREATE TABLE run_log (id INTEGER PRIMARY KEY, run_type TEXT, complete INTEGER,'
                ' parser_version INTEGER)')
    cache = sqlite3.connect(':memory:')
    meta = {'data/media/0/Download/x.zip': {'mtime': 1700000000},
            'data/media/0/Download/x.zip/a.jpg': {'mtime': 1600000000, '_archive_path':
                                                  'data/media/0/Download/x.zip'},
            'data/data/c/cache/0123456789abcdef_0/img.jpg': {'mtime': 1700000500,
                                                             'source_url': 'https://e.x/i.jpg'}}
    files = {'key': 'k', 'collect': lambda: {
        'paths': ['data/media/0/Download/x.zip/a.jpg'], 'metadata': meta,
        'time_cols': [('Modified', 'mtime')], 'ignored': 0,
        'cache_paths': ['data/data/c/cache/0123456789abcdef_0/img.jpg'],
        'archives': {'data/media/0/Download/x.zip': (3, 1)}}}
    timeline.build(res, cache, {}, files=files)
    rows, _ = timeline.query(cache)
    C = timeline.COLUMNS
    got = {r[C.index('script')]: r for r in rows}
    # the file inside the archive: no event from its time inside the zip
    assert timeline.FILES_SCRIPT not in got
    arc = got[timeline.ARCHIVE_SCRIPT]
    assert arc[0] == 1700000000000 and 'Archive arrived on the device: x.zip, 3 file(s) (1 pictures/videos)' == arc[C.index('event')]
    assert 'arrived with it' in arc[C.index('caution')]
    c = got[timeline.CACHE_SCRIPT]
    assert c[0] == 1700000500000 and 'https://e.x/i.jpg' in c[C.index('detail')]
    assert 'cache record' in c[C.index('caution')]
    note = next(s for s in timeline.source_list(cache)
                if s[0] == timeline.FILES_SCRIPT and s[2] == 'Modified')[9]
    assert '1 files inside unpacked archives' in note


def test_detailed_mode_is_its_own_source():
    import sqlite3
    res = sqlite3.connect(':memory:')
    res.execute('CREATE TABLE run_log (id INTEGER PRIMARY KEY, run_type TEXT, complete INTEGER,'
                ' parser_version INTEGER)')
    meta = {'data/media/0/DCIM/a.jpg': {'mtime': 1700000000},
            'data/data/com.x/databases/x.db': {'mtime': 1700000100, 'atime': 1700000200}}
    def files(detail):
        return {'key': 'k', 'collect': lambda: {
            'paths': ['data/media/0/DCIM/a.jpg'], 'metadata': meta,
            'time_cols': [('Modified', 'mtime'), ('Accessed', 'atime')], 'ignored': 0,
            **({'detail_paths': ['data/data/com.x/databases/x.db']} if detail else {})}}
    cache = sqlite3.connect(':memory:')
    timeline.build(res, cache, {}, files=files(False))
    assert timeline.count(cache) == 1
    timeline.build(res, cache, {}, files=files(True))
    rows, _ = timeline.query(cache, scripts=[timeline.DETAIL_SCRIPT])
    C = timeline.COLUMNS
    assert len(rows) == 1 and rows[0][0] == 1700000100000         # no Accessed event
    assert rows[0][C.index('source')] == timeline.DETAIL_SOURCE
    assert 'Detailed mode' in rows[0][C.index('caution')]
    assert timeline.count(cache) == 2
