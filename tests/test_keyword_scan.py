"""keyword_scan: matching inside one file, across read boundaries, and the
worker-process job over a real archive layout."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))
import keyword_scan as ks  # noqa: E402


def _split(data, size):
    return [data[i:i + size] for i in range(0, len(data), size)]


def _offsets(hits):
    return [o for o, _ in hits]


def test_hits_found_once_whatever_the_read_size():
    pats = ks.make_patterns('Test')
    data = (b'..test..TEST' + 'test'.encode('utf-16-le') + b'x' * 50
            + 'TeSt'.encode('utf-16-be') + b'tes' + b't')
    whole = ks.search_blocks([data], pats)
    assert len(whole) == 5
    for size in (1, 2, 3, 5, 7, 8, 13, 64):
        assert sorted(_offsets(ks.search_blocks(_split(data, size), pats))) == sorted(_offsets(whole)), size


def test_context_marks_the_hit():
    pats = ks.make_patterns('needle')
    [(off, ctx)] = ks.search_blocks([b'hay hay NEEDLE hay'], pats, ctx_bytes=4)
    assert off == 8 and ctx == 'hay [NEEDLE] hay'


def test_jobs_keep_archive_order_and_limits():
    entries = [('c', 300, 10), ('a', 100, 10), ('b', 200, 10), ('big', 400, 100)]
    jobs = ks.make_jobs(entries, job_bytes=25, job_files=10)
    assert [[e[0] for e in j] for j in jobs] == [['a', 'b'], ['c'], ['big']]


def test_search_job_reads_each_file_by_offset_only(tmp_path):
    # Two "stored files" side by side: a match spanning their border must
    # not be reported (a hit never crosses from one file into the next).
    blob = b'HEADER' + b'aaaTE' + b'STbbb' + b'zz test zz'
    p = tmp_path / 'a.zip'
    p.write_bytes(blob)
    entries = [('one', 6, 5), ('two', 11, 5), ('three', 16, 10)]
    out, bad = ks.search_job(str(p), entries, ks.make_patterns('test'), chunk=3)
    assert bad == 0
    [(name, [(off, ctx)])] = out
    # (context stops at the end of the current read: here 3 bytes)
    assert name == 'three' and off == 3 and ctx.startswith('zz [test]')
