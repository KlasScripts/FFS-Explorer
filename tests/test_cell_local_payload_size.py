"""Boundary-value tests for sqlite_carve._cell_local_payload_size --
the overflow-threshold formula from the SQLite file format spec that
locate_live_row/locate_offset rely on to report a correct, non-overrunning
on-page byte span for an overflowing cell (see that function's own
docstring for the real bug this formula fixes)."""
from sqlite_carve import _cell_local_payload_size


def test_payload_fits_inline_below_threshold():
    usable_size = 4096
    x = usable_size - 35
    size, overflows = _cell_local_payload_size(x - 1, usable_size)
    assert size == x - 1
    assert overflows is False


def test_payload_exactly_at_threshold_does_not_overflow():
    usable_size = 4096
    x = usable_size - 35
    size, overflows = _cell_local_payload_size(x, usable_size)
    assert size == x
    assert overflows is False


def test_payload_one_byte_over_threshold_overflows():
    usable_size = 4096
    x = usable_size - 35
    size, overflows = _cell_local_payload_size(x + 1, usable_size)
    assert overflows is True
    assert size < x + 1
    assert size > 0


def test_large_payload_overflows_with_bounded_local_size():
    usable_size = 4096
    size, overflows = _cell_local_payload_size(1_000_000, usable_size)
    assert overflows is True
    # local_size must never exceed the inline threshold -- a caller
    # highlighting `size` bytes from the cell's own page must never run
    # past the real on-page content (see the function's own docstring).
    assert size <= usable_size - 35
    assert size > 0
