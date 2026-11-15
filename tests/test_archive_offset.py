"""Archive offset (archive_offset.py): the device's likely UTC offset from
the zip's DOS time (local wall clock) vs its UT mtime (UTC). Display only."""
import calendar
import json
import struct
import time
from types import SimpleNamespace

import archive_offset as a


def _entry(ut, offset_s, name='f', ut_present=True):
    """A zip entry whose DOS time is the local wall clock at *offset_s*."""
    local = time.gmtime(ut + offset_s)
    extra = struct.pack('<HHBI', 0x5455, 5, 1, ut) if ut_present else b''
    return SimpleNamespace(filename=name, date_time=tuple(local[:6]), extra=extra)


def _pairs(entries):
    return a.sample_pairs(entries)


JAN, JUL = 1704067200, 1719792000      # 2024-01-01, 2024-07-01 UTC


def test_fixed_offset():
    es = [_entry(JAN + i * 3600, -5 * 3600) for i in range(300)]
    r = a.detect(_pairs(es), zones=['America/Bogota', 'Europe/London'])
    assert r['status'] == 'fixed' and r['offset_s'] == -18000 and r['n'] == 300
    assert r['zones'] == ['America/Bogota']
    assert a.offset_text(r['offset_s']) == 'UTC−05:00'
    assert a.offset_text(19800) == 'UTC+05:30'


def test_daylight_saving_is_not_a_fixed_offset():
    # Eastern: −5 in January, −4 in July. 90.5% / 9.5% must still be DST
    # (the Android 15 test phone's split), never "fixed −04:00".
    es = ([_entry(JUL + i * 3600, -4 * 3600) for i in range(905)]
          + [_entry(JAN + i * 3600, -5 * 3600) for i in range(95)])
    r = a.detect(_pairs(es), zones=['America/New_York', 'America/Bogota', 'America/Halifax'])
    assert r['status'] == 'dst' and r['offset_s'] is None
    assert r['zones'] == ['America/New_York']          # matches each file at its own date


def test_no_consensus_offers_nothing():
    es = ([_entry(JAN + i, -5 * 3600) for i in range(150)]
          + [_entry(JAN + i, 2 * 3600) for i in range(150)])
    assert a.detect(_pairs(es), zones=[])['status'] == 'none'
    few = [_entry(JAN + i, -5 * 3600) for i in range(50)]          # below MIN_SAMPLES
    assert a.detect(_pairs(few), zones=[])['status'] == 'none'


def test_ut_zero_missing_and_unset_dos_are_not_sampled():
    zero = [_entry(0, 0) for _ in range(200)]
    for e in zero:
        e.date_time = (2024, 1, 1, 0, 0, 0)
    no_ut = [_entry(JAN, -5 * 3600, ut_present=False) for _ in range(200)]
    unset = [SimpleNamespace(filename='f', date_time=(1980, 1, 1, 0, 0, 0),
                             extra=struct.pack('<HHBI', 0x5455, 5, 1, JAN)) for _ in range(200)]
    folders = [_entry(JAN, -5 * 3600, name='d/') for _ in range(200)]
    assert _pairs(zero + no_ut + unset + folders) == []
    assert a.ut_mtime(struct.pack('<HHBI', 0x5455, 5, 1, 0)) == 0
    assert a.ut_mtime(struct.pack('<HHB', 0x5455, 1, 0)) is None     # flags without mtime
    assert a.ut_mtime(struct.pack('<HH', 0x7875, 0)) is None


def test_format_fixed():
    assert a.format_fixed(JAN, -18000) == '2023-12-31 19:00:00 UTC−05:00'
    assert a.format_fixed(JAN * 10**9, -18000) == '2023-12-31 19:00:00 UTC−05:00'  # APFS ns


def test_setting_round_trip_and_display(tmp_path):
    from db_utils import _open_results_db, save_case_setting
    from timestamp_display import TimestampDisplayMixin
    det = {'status': 'fixed', 'offset_s': -18000, 'n': 300, 'bins': [[-18000, 300]],
           'zones': ['America/Bogota']}
    with _open_results_db(str(tmp_path)) as db:
        save_case_setting(db, 'archive_offset', json.dumps(det))
        save_case_setting(db, 'archive_zone_choice', 'fixed')
        got = TimestampDisplayMixin._load_or_detect_archive_offset(db, '/nope.zip', str(tmp_path))
    assert got['detect']['offset_s'] == -18000 and got['choice'] == 'fixed'

    o = TimestampDisplayMixin.__new__(TimestampDisplayMixin)
    o._archive_offset_detect, o._archive_zone_choice = got['detect'], 'fixed'
    o._timestamp_display_mode = 'archive'
    assert o.format_ts(JAN) == '2023-12-31 19:00:00 UTC−05:00'
    assert o._timestamp_mode_text() == 'Local via archive DOS−UT (UTC−05:00, fixed)'
    o._archive_zone_choice = 'America/Bogota'
    assert o.format_ts(JAN) == '2023-12-31 19:00:00 -05'
    assert 'America/Bogota, a zone consistent with the zip' in o._timestamp_mode_text()
    # never "Handset", and nothing chosen falls back to UTC
    o._archive_zone_choice = ''
    assert o._timestamp_mode_text() == 'UTC' and o.format_ts(JAN).endswith(' UTC')
    o._timestamp_display_mode = 'utc'
    o._archive_zone_choice = 'fixed'
    assert o.format_ts(JAN).endswith(' UTC')        # opt-in only


def test_detection_is_stored_once(tmp_path, monkeypatch):
    from db_utils import _open_results_db, load_case_setting
    from timestamp_display import TimestampDisplayMixin
    calls = []
    monkeypatch.setattr(a, 'detect_from_archive',
                        lambda z, c: calls.append(1) or {'status': 'none', 'n': 0})
    with _open_results_db(str(tmp_path)) as db:
        TimestampDisplayMixin._load_or_detect_archive_offset(db, 'z', str(tmp_path))
        TimestampDisplayMixin._load_or_detect_archive_offset(db, 'z', str(tmp_path))
        assert json.loads(load_case_setting(db, 'archive_offset'))['status'] == 'none'
    assert calls == [1]
