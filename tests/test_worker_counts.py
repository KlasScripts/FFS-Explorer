"""worker_counts (A-98): automatic and set counts for the CPU-heavy and
disk-heavy stages."""
import worker_counts as wc


def test_automatic_and_set(monkeypatch):
    monkeypatch.setattr(wc, 'physical_cores', lambda: 20)
    monkeypatch.setattr(wc, 'logical_cores', lambda: 40)
    assert wc.resolve(0, 0) == (19, 12)            # Threadripper: 20 cores, 40 threads
    assert wc.resolve(None, '') == (19, 12)
    assert wc.resolve(30, 6) == (30, 6)            # a set number is used as it is
    assert wc.resolve(99, 99) == (40, 40)          # up to the logical cores
    monkeypatch.setattr(wc, 'physical_cores', lambda: 10)
    monkeypatch.setattr(wc, 'logical_cores', lambda: 10)
    assert wc.resolve(0, 0) == (9, 10)             # the Mac this was measured on
    monkeypatch.setattr(wc, 'physical_cores', lambda: 1)
    monkeypatch.setattr(wc, 'logical_cores', lambda: 1)
    assert wc.resolve(0, 0) == (1, 1)
