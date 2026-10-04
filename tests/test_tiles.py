"""A note array's grid sliced per Note and the identify flash.

Moved from FiestaBoard core's ``tests/test_devices.py`` when core's copies of
these helpers (``slice_note_array_grid``, ``stitch_note_array_grid``,
``identify_pattern``) were removed as dead code (Phase 4 P4e): the tile array
here is the only one.
"""

from __future__ import annotations

import pytest

from plugins.vestaboard.tiles import identify_pattern, slice_grid, stitch_grid
from plugins.vestaboard.transport import NOTE_COLS, NOTE_ROWS


def _grid(notes_wide, notes_tall):
    rows, cols = notes_tall * NOTE_ROWS, notes_wide * NOTE_COLS
    return [[r * 1000 + c for c in range(cols)] for r in range(rows)]


@pytest.mark.parametrize("w,h", [(1, 1), (2, 1), (1, 2), (2, 2), (4, 1), (8, 8)])
def test_slice_stitch_round_trip(w, h):
    grid = _grid(w, h)
    subgrids = slice_grid(grid, w, h)
    assert len(subgrids) == w * h
    assert stitch_grid(subgrids, w, h) == grid


def test_tile_0_1_gets_cols_15_to_29():
    grid = _grid(2, 1)
    sub = slice_grid(grid, 2, 1)[(0, 1)]
    assert len(sub) == NOTE_ROWS
    assert all(len(r) == NOTE_COLS for r in sub)
    assert sub[0] == grid[0][15:30]
    assert sub[2] == grid[2][15:30]


def test_tile_1_0_gets_rows_3_to_5():
    grid = _grid(1, 2)
    assert slice_grid(grid, 1, 2)[(1, 0)] == [grid[3], grid[4], grid[5]]


def test_stitch_fills_missing_slots():
    grid = _grid(2, 1)
    subgrids = slice_grid(grid, 2, 1)
    del subgrids[(0, 1)]
    stitched = stitch_grid(subgrids, 2, 1, fill=0)
    assert stitched[0][:15] == grid[0][:15]
    assert stitched[0][15:] == [0] * 15


def test_stitch_ignores_out_of_range_and_malformed():
    stitched = stitch_grid({(5, 5): [[1] * NOTE_COLS] * NOTE_ROWS, (0, 0): [[1] * 3]}, 1, 1)
    assert stitched == [[0] * NOTE_COLS for _ in range(NOTE_ROWS)]


def test_the_identify_flash_is_note_sized():
    pattern = identify_pattern(0, 0, notes_wide=2)
    assert len(pattern) == NOTE_ROWS
    assert all(len(r) == NOTE_COLS for r in pattern)


def test_distinct_slots_produce_distinct_flashes():
    assert identify_pattern(0, 0, 2) != identify_pattern(0, 1, 2)
    assert identify_pattern(0, 1, 2) != identify_pattern(1, 0, 2)
