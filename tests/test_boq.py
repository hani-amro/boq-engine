# -*- coding: utf-8 -*-
"""Correctness tests for the BOQ core. Run:  python -m pytest tests/ -q

Every quantity this product prints is money somebody either pays or eats, so the
tests below are deliberately NOT smoke tests. Each golden case is worked out by
hand in its own docstring — dimensions, deduction, formula, waste — so a quantity
surveyor can re-do the arithmetic on paper and either agree with the engine or
prove it wrong. If a number here changes, either a convention changed on purpose
(and the docstring must change with it) or the engine broke.

Layout:
  1. geometry            — shoelace, perimeter, net wall area, an L-shaped room
  2. golden cases        — one fully hand-calculated line per finish family
  3. deduction threshold — the per-opening convention, above and below the limit
  4. skirting            — doors come off the run, windows do not
  5. waste               — net vs gross, and an override that lands in the audit trail
  6. safe evaluator      — adversarial: no attributes, no dunders, no calls, no imports
  7. pricing             — versioned books, the totals cascade, the placeholder gate
  8. validation          — zero dimensions, impossible openings, absurd heights
  9. round-trip          — Project.to_dict() -> from_dict() loses nothing
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.schema import (
    FinishAssignment, GeometryError, Opening, Project, Space, Wall, price_line,
)
from core.boq_engine import is_warning, summarize, takeoff, takeoff_space, warnings_of
from core import rules_loader
from core.rules_loader import (
    ALLOWED_FUNCS, FormulaError, RuleError, load_rules, safe_eval, validate_rules,
)
from core import pricing
from core.pricing import (
    BookError, PlaceholderRatesError, QuoteTerms, apply_terms, assert_client_ready,
    client_block_reason_ar, load_book, price_one, quote,
)
from core import proposal


RULES = load_rules()

# The reference room used by most golden cases: a plain 5.00 × 4.00 m salon,
# clear height 3.00 m, with one door and one window.
#   polygon  (0,0) (5,0) (5,4) (0,4)
#   area     5 × 4                        = 20.00 m²
#   perimeter 5 + 4 + 5 + 4               = 18.00 m
#   walls    W1 5.00, W2 4.00, W3 5.00, W4 4.00, all 3.00 m high
#   D1 door   1.00 × 2.10 = 2.10 m²  (on W1)
#   N1 window 1.20 × 1.40 = 1.68 m²  (on W2)
ROOM_POLY = [(0.0, 0.0), (5.0, 0.0), (5.0, 4.0), (0.0, 4.0)]


def _door(id_="D1", w=1.00, h=2.10, n=1):
    return Opening(id=id_, kind="door", width_m=w, height_m=h, count=n)


def _window(id_="N1", w=1.20, h=1.40, n=1):
    return Opening(id=id_, kind="window", width_m=w, height_m=h, count=n)


def room(finishes=None, openings=None, *, height=3.0, type_="living",
         polygon=ROOM_POLY, floor_area=None):
    """The reference 5×4×3 room. ``openings`` go on W1/W2 in the order given."""
    ops = [_door(), _window()] if openings is None else list(openings)
    walls = [
        Wall(id="W1", length_m=5.0, height_m=height, openings=ops[:1]),
        Wall(id="W2", length_m=4.0, height_m=height, openings=ops[1:]),
        Wall(id="W3", length_m=5.0, height_m=height),
        Wall(id="W4", length_m=4.0, height_m=height),
    ]
    return Space(id="S1", name="صالون", type=type_, height_m=height, walls=walls,
                 polygon=(list(polygon) if polygon else None), floor_area_m2=floor_area,
                 finishes=list(finishes or []))


def one_line(space, rule_key, spec=None, overrides=None):
    """Measure exactly one finish and return its BOQLine (never a warning line)."""
    space.finishes = [FinishAssignment(surface=RULES[rule_key]["surface"],
                                       system=rule_key, spec=dict(spec or {}))]
    lines = takeoff_space(space, rules=RULES, overrides=overrides)
    real = [ln for ln in lines if not is_warning(ln)]
    assert len(real) == 1, [ln.breakdown for ln in lines]
    return real[0]


# =========================================================================== #
# 1. Geometry                                                                  #
# =========================================================================== #
def test_shoelace_area_and_perimeter():
    """Rectangle 5×4: area 20.00 m², perimeter 5+4+5+4 = 18.00 m.

    The ring may be given open or closed — a duplicated closing vertex must not
    add a zero-length side, and must not change the area.
    """
    s = room()
    assert s.floor_area == 20.0
    assert s.perimeter_m == 18.0

    closed = room(polygon=ROOM_POLY + [(0.0, 0.0)])
    assert closed.floor_area == 20.0
    assert closed.perimeter_m == 18.0

    # Winding direction is irrelevant: |signed area| is the measurement.
    cw = room(polygon=list(reversed(ROOM_POLY)))
    assert cw.floor_area == 20.0
    print("ok  shoelace: 5×4 -> 20.00 m², 18.00 m (open, closed, both windings)")


def test_l_shaped_room_is_not_a_rectangle():
    """L-shape (0,0)(6,0)(6,4)(3,4)(3,7)(0,7).

    shoelace Σ(x1·y2 − x2·y1)
        = (0·0−6·0) + (6·4−6·0) + (6·4−3·4) + (3·7−3·4) + (3·7−0·7) + (0·0−0·7)
        = 0 + 24 + 12 + 9 + 21 + 0 = 66      → area = 66/2 = 33.00 m²
    cross-check by decomposition: 6×4 (y 0→4) + 3×3 (y 4→7) = 24 + 9 = 33.00 m² ✓
    perimeter = 6 + 4 + 3 + 3 + 3 + 7 = 26.00 m
    The bounding box would be 6×7 = 42 m², so a naive rectangle over-measures by
    9 m² — this is exactly the failure the shoelace exists to prevent.
    """
    poly = [(0.0, 0.0), (6.0, 0.0), (6.0, 4.0), (3.0, 4.0), (3.0, 7.0), (0.0, 7.0)]
    s = Space(id="L1", name="ممر على شكل L", type="living", height_m=3.0, polygon=poly)
    assert s.floor_area == 33.0
    assert s.perimeter_m == 26.0
    assert s.floor_area != 6.0 * 7.0
    assert s.gross_wall_area == 78.0          # 26.00 × 3.00
    print("ok  L-shaped room: 33.00 m² (not the 42.00 m² bounding box), 26.00 m")


def test_perimeter_polygon_vs_walls():
    """Polygon wins over the wall list — the drawing beats the typed assumption.

    No polygon: perimeter = Σ wall lengths = 5 + 4 + 5 + 4 = 18.00 m.
    With a polygon whose walls were mis-entered (Σ = 30.00 m), the polygon's
    18.00 m is still used.
    """
    from_walls = room(polygon=None, floor_area=20.0)
    assert from_walls.perimeter_m == 18.0
    assert from_walls.floor_area == 20.0      # falls back to the entered area

    wrong_walls = room()
    for w, L in zip(wrong_walls.walls, (9.0, 6.0, 9.0, 6.0)):
        w.length_m = L
    assert sum(w.length_m for w in wrong_walls.walls) == 30.0
    assert wrong_walls.perimeter_m == 18.0    # polygon is the authority

    # Neither polygon nor walls: refuse, never return a silent zero.
    empty = Space(id="X", name="فراغ فارغ", type="living", height_m=3.0)
    with pytest.raises(GeometryError):
        _ = empty.perimeter_m
    with pytest.raises(GeometryError):
        _ = empty.floor_area
    print("ok  perimeter: polygon 18.00 m beats walls 30.00 m; no geometry -> raises")


def test_net_wall_area_with_openings():
    """gross = perimeter × height = 18.00 × 3.00 = 54.00 m²
       holes = door 1.00×2.10 = 2.10 + window 1.20×1.40 = 1.68  = 3.78 m²
       net   = 54.00 − 3.78 = 50.22 m²
    A niche is a RECESS, not a hole: it must never come off the net area.
    """
    s = room()
    assert s.gross_wall_area == 54.0
    assert s.openings_area == 3.78
    assert s.net_wall_area == 50.22
    assert s.walls[0].gross_area_m2 == 15.0 and s.walls[0].net_area_m2 == 12.9

    with_niche = room(openings=[_door(), _window(),
                                Opening(id="K1", kind="niche", width_m=0.6, height_m=0.9)])
    with_niche.walls[2].openings = [with_niche.walls[1].openings.pop()]
    assert with_niche.openings_area == 3.78          # unchanged: the niche is not a hole
    assert with_niche.niches_area == 0.54            # 0.60 × 0.90
    assert with_niche.net_wall_area == 50.22

    # count multiplies: 2 identical doors = 4.20 m² off the wall
    twin = room(openings=[_door(n=2), _window()])
    assert twin.openings_area == 4.20 + 1.68
    print("ok  net wall area: 54.00 − 3.78 = 50.22 m² (niches excluded, count applied)")


# =========================================================================== #
# 2. Golden cases — one hand-calculated line per finish family                  #
# =========================================================================== #
def test_golden_floor_tile():
    """ceramic_tile_floor (TIL-CER-FLR), 5×4 salon with one 1.00 m door.

    formula: floor_area − manual_deduct_area + door_reveal_strip
      floor_area        = shoelace(5×4)                    = 20.00 m²
      manual_deduct_area= deduct_areas_m2 is empty          =  0.00 m²
      door_reveal_strip = Σ door widths × reveal depth
                        = 1.00 m × 0.20 m                   =  0.20 m²
      net   = 20.00 − 0.00 + 0.20                           = 20.20 m²
      waste = 10% (rule waste_pct, straight lay -> no uplift)
      gross = 20.20 × 1.10 = 22.22                          = 22.22 m²
    """
    ln = one_line(room(), "ceramic_tile_floor")
    assert ln.code == "TIL-CER-FLR" and ln.unit == "m2"
    assert ln.net_qty == 20.20
    assert ln.waste_pct == 0.10
    assert ln.gross_qty == 22.22
    assert any("20.00" in b and "0.20" in b for b in ln.breakdown)


def test_golden_wall_paint():
    """paint_emulsion_internal_wall (PNT-EMU-WALL), same salon.

    formula: gross_wall_area − deductible_openings_area
             + reveals_on × deductible_reveal_run × reveal_width_m
             − tiled_wall_area − skirting_band_area
      gross_wall_area          = 18.00 m × 3.00 m           = 54.00 m²
      deduction threshold      = 0.50 m² (rule default)
      door   1.00×2.10 = 2.10 m² ≥ 0.50  -> deducted
      window 1.20×1.40 = 1.68 m² ≥ 0.50  -> deducted
      deductible_openings_area = 2.10 + 1.68                =  3.78 m²
      reveals_on               = 0  (paint declares add_reveals: false — the reveal
                                     is painted out of the same m² rate)
      tiled_wall_area          = 0 (no cladding measured in the room)
      skirting_band_area       = 0 (deduct_skirting_band: false)
      net   = 54.00 − 3.78                                  = 50.22 m²
      waste = 5%
      gross = 50.22 × 1.05 = 52.731 -> 52.73                = 52.73 m²
    """
    ln = one_line(room(), "paint_emulsion_internal_wall")
    assert ln.code == "PNT-EMU-WALL" and ln.unit == "m2"
    assert ln.net_qty == 50.22
    assert ln.waste_pct == 0.05
    assert ln.gross_qty == 52.73


def test_golden_gypsum_ceiling():
    """gypsum_board_ceiling_flat (GYP-CEIL-FLT), same salon, two ceiling voids.

    formula: floor_area − ceiling_void_area
      floor_area   = 20.00 m²
      ceiling voids declared: 1.20 m² and 0.40 m²
      ceiling deduction threshold = 1.00 m² (ceiling_deduct_threshold_m2)
        1.20 ≥ 1.00 -> deducted
        0.40 < 1.00 -> NOT deducted (the board is cut round it, the offcut is waste)
      ceiling_void_area = 1.20                              =  1.20 m²
      net   = 20.00 − 1.20                                  = 18.80 m²
      waste = 10%
      gross = 18.80 × 1.10 = 20.68                          = 20.68 m²
    """
    ln = one_line(room(), "gypsum_board_ceiling_flat",
                  spec={"ceiling_openings_m2": [1.20, 0.40]})
    assert ln.code == "GYP-CEIL-FLT" and ln.unit == "m2"
    assert ln.net_qty == 18.80
    assert ln.waste_pct == 0.10
    assert ln.gross_qty == 20.68
    # the un-deducted void must be explained, not silently dropped
    assert any("0.40" in b for b in ln.breakdown)


def test_golden_skirting():
    """ceramic_skirting (TIL-CER-SKT), same salon.

    formula: wet_room_factor × (perimeter_m − skirting_door_deduct
             − full_height_window_run − fitted_units_len_m − wall_tiled_len_m
             + reveals_on × (door_jamb_returns + column_returns_m))
      wet_room_factor        = 1 (a salon is not a wet room)
      perimeter_m            = 18.00 m
      skirting_door_deduct   = door width, in FULL, no threshold  = 1.00 m
      full_height_window_run = 0 (window_to_floor is off; the skirting runs
                                  straight underneath a sill-height window)
      fitted units / tiled length / returns = 0
      net   = 1 × (18.00 − 1.00)                            = 17.00 m
      waste = 10%
      gross = 17.00 × 1.10 = 18.70                          = 18.70 m
    """
    ln = one_line(room(), "ceramic_skirting")
    assert ln.code == "TIL-CER-SKT" and ln.unit == "m"
    assert ln.net_qty == 17.00
    assert ln.waste_pct == 0.10
    assert ln.gross_qty == 18.70


def test_golden_plaster():
    """plaster_internal_wall_cement (PLS-INT-WALL), same salon.

    formula: gross_wall_area − deductible_openings_area
             + reveals_on × deductible_reveal_run × reveal_width_m
             + niche_area − tiled_wall_area
      gross_wall_area          = 18.00 × 3.00                = 54.00 m²
      deductible_openings_area = 2.10 + 1.68                 =  3.78 m²
      reveals_on               = 1 (add_reveals true, reveal_mode = area)
      deductible_reveal_run:
        door   (no sill reveal, the floor runs through) 2×2.10 + 1.00 = 5.20 m
        window (all four sides) 2×1.40 + 2×1.20        = 2.80 + 2.40 = 5.20 m
        total                                                = 10.40 m
      reveal_width_m           = 0.15 m
      reveal area              = 10.40 × 0.15               =  1.56 m²
      niche_area / tiled_wall_area = 0
      net   = 54.00 − 3.78 + 1.56                            = 51.78 m²
      waste = 10%
      gross = 51.78 × 1.10 = 56.958 -> 56.96                 = 56.96 m²
    """
    ln = one_line(room(), "plaster_internal_wall_cement")
    assert ln.code == "PLS-INT-WALL" and ln.unit == "m2"
    assert ln.net_qty == 51.78
    assert ln.waste_pct == 0.10
    assert ln.gross_qty == 56.96
    assert any("51.78" in b for b in ln.breakdown)


def test_golden_lines_are_deterministic_and_summarize():
    """The five golden lines together: the bill adds up to what it prints.

    floor 20.20 m² + ceiling 20.00 m² = 40.20 m² of م٢ floor/ceiling work,
    walls 50.22 (paint) + 51.78 (plaster) = 102.00 m², skirting 17.00 م.ط.
    م٢ and م.ط are never added together.
    """
    s = room(finishes=[
        FinishAssignment(surface="floor", system="ceramic_tile_floor"),
        FinishAssignment(surface="wall", system="plaster_internal_wall_cement"),
        FinishAssignment(surface="wall", system="paint_emulsion_internal_wall"),
        FinishAssignment(surface="ceiling", system="gypsum_board_ceiling_flat"),
        FinishAssignment(surface="skirting", system="ceramic_skirting"),
    ])
    lines = takeoff_space(s, rules=RULES)
    got = {ln.rule_key: ln.net_qty for ln in lines if not is_warning(ln)}
    assert got == {
        "ceramic_tile_floor": 20.20,
        "plaster_internal_wall_cement": 51.78,
        "paint_emulsion_internal_wall": 50.22,
        "gypsum_board_ceiling_flat": 20.00,
        "ceramic_skirting": 17.00,
    }
    # order is the order the architect authored the finishes, not measurement order
    assert [ln.rule_key for ln in lines if not is_warning(ln)] == [
        "ceramic_tile_floor", "plaster_internal_wall_cement",
        "paint_emulsion_internal_wall", "gypsum_board_ceiling_flat", "ceramic_skirting"]

    summary = summarize(lines)
    assert summary["by_unit"]["m2"]["net_qty"] == round(20.20 + 51.78 + 50.22 + 20.00, 2)
    assert summary["by_unit"]["m"]["net_qty"] == 17.00
    assert summary["totals"]["lines"] == 5

    # determinism: same input, same numbers, twice
    again = takeoff_space(room(finishes=list(s.finishes)), rules=RULES)
    assert [(l.code, l.net_qty, l.gross_qty) for l in again] == \
           [(l.code, l.net_qty, l.gross_qty) for l in lines]
    print("ok  golden set: 5 lines, م٢ and م.ط summed separately, reproducible")


# =========================================================================== #
# 3. The deduction threshold convention                                        #
# =========================================================================== #
def test_deduction_threshold_above_and_below():
    """The threshold is tested against ONE opening's area, not the room's total.

    Two windows in the same room, plaster (threshold 0.50 m²):
      A 0.70 × 0.80 = 0.56 m² ≥ 0.50 -> deducted
      B 0.60 × 0.60 = 0.36 m² < 0.50 -> NOT deducted
      deductible_openings_area = 0.56
      deductible_reveal_run    = A only: 2×0.80 + 2×0.70 = 1.60 + 1.40 = 3.00 m
      reveal area              = 3.00 × 0.15 = 0.45 m²
      net = 54.00 − 0.56 + 0.45 = 53.89 m²   (gross = ×1.10 = 59.279 -> 59.28)
    Note the golden rule that follows from it: B's jambs are NOT measured either,
    because B was never deducted — the reveal follows the deduction.
    """
    ops = [_window("A", 0.70, 0.80), _window("B", 0.60, 0.60)]
    ln = one_line(room(openings=ops), "plaster_internal_wall_cement")
    assert ln.net_qty == 53.89
    assert ln.gross_qty == 59.28
    assert any("B" in b and "0.36" in b for b in ln.breakdown)


def test_deduction_threshold_is_per_opening_not_on_the_total():
    """Six small openings, 0.36 m² each: total 2.16 m² — still nothing deducted.

    2.16 m² is more than four times the 0.50 m² threshold, so a threshold applied
    to the SUM would wrongly remove 2.16 m² of plaster from the paid quantity.
    The convention is per opening:
      each 0.60 × 0.60 = 0.36 m² < 0.50 -> not deducted
      net = gross_wall_area = 54.00 m² exactly, with zero reveals measured.
    Count is part of one opening, not a way round the test: a single record of
    count = 6 must behave identically to six records.
    """
    many = [_window(f"N{i}", 0.60, 0.60) for i in range(6)]
    s = room(openings=many[:1])
    s.walls[1].openings = many[1:]
    assert s.openings_area == 2.16                       # the holes really are there
    ln = one_line(s, "plaster_internal_wall_cement")
    assert ln.net_qty == 54.00

    counted = room(openings=[_window("N0", 0.60, 0.60, n=6)])
    counted.walls[1].openings = []
    assert counted.openings_area == 2.16
    assert one_line(counted, "plaster_internal_wall_cement").net_qty == 54.00
    print("ok  threshold: per-opening (6 × 0.36 = 2.16 m² deducts nothing)")


def test_deduction_threshold_is_overridable_per_rule():
    """A convention, not physics — the office must be able to move it, loudly.

    Threshold raised to 2.00 m² on the plaster rule only:
      door   2.10 m² ≥ 2.00 -> still deducted
      window 1.68 m² < 2.00 -> no longer deducted
      deductible_reveal_run = door only = 2×2.10 + 1.00 = 5.20 m
      net = 54.00 − 2.10 + 5.20 × 0.15 = 54.00 − 2.10 + 0.78 = 52.68 m²
    """
    ln = one_line(room(), "plaster_internal_wall_cement",
                  overrides={"plaster_internal_wall_cement": {"deduct_threshold_m2": 2.0}})
    assert ln.net_qty == 52.68
    assert any("تعديل يدوي معتمد" in b for b in ln.breakdown)

    # An out-of-range override is refused and the rule value stands (net 51.78).
    refused = one_line(room(), "plaster_internal_wall_cement",
                       overrides={"*": {"deduct_threshold_m2": 999.0}})
    assert refused.net_qty == 51.78
    assert any("مرفوض" in b for b in refused.breakdown)
    print("ok  threshold override: 0.50 -> 2.00 m² applied; 999 m² refused")


# =========================================================================== #
# 4. Skirting: doors come off, windows do not                                  #
# =========================================================================== #
def test_skirting_subtracts_doors_not_windows():
    """A skirting stops at a door and runs straight under a window.

    perimeter 18.00 m, one 1.00 m door, one 1.20 m sill-height window:
      net = 18.00 − 1.00 = 17.00 m     (the window's 1.20 m is NOT removed)
    Widening the window to 2.50 m must not move the number by one millimetre;
    widening the DOOR to 1.60 m must move it by exactly 0.60 m.
    An arch is a doorway too, so it is deducted like one.
    """
    base = one_line(room(), "ceramic_skirting").net_qty
    assert base == 17.00

    wide_window = one_line(room(openings=[_door(), _window(w=2.50)]), "ceramic_skirting")
    assert wide_window.net_qty == 17.00

    wide_door = one_line(room(openings=[_door(w=1.60), _window()]), "ceramic_skirting")
    assert wide_door.net_qty == 16.40                    # 18.00 − 1.60

    with_arch = room(openings=[_door(), _window()])
    with_arch.walls[2].openings = [Opening(id="Q1", kind="arch", width_m=1.30,
                                           height_m=2.60)]
    assert one_line(with_arch, "ceramic_skirting").net_qty == 15.70   # 18 − 1.00 − 1.30


def test_skirting_deducts_a_floor_reaching_window_only_when_named():
    """A French window DOES kill the skirting under it — but only the named one.

    window_to_floor_ids = ["N1"] with N1 1.20 m wide:
      net = 18.00 − 1.00 (door) − 1.20 (N1)             = 15.80 m
    A second, ordinary window in the same room keeps its skirting.
    """
    s = room(openings=[_door(), _window("N1", 1.20, 2.40)])
    s.walls[2].openings = [_window("N2", 1.50, 1.40)]
    plain = one_line(s, "ceramic_skirting")
    assert plain.net_qty == 17.00                        # neither window deducted

    s2 = room(openings=[_door(), _window("N1", 1.20, 2.40)])
    s2.walls[2].openings = [_window("N2", 1.50, 1.40)]
    named = one_line(s2, "ceramic_skirting", spec={"window_to_floor_ids": ["N1"]})
    assert named.net_qty == 15.80                        # only N1, not N2

    s3 = room(openings=[_door(), _window("N1", 1.20, 2.40)])
    s3.walls[2].openings = [_window("N2", 1.50, 1.40)]
    blanket = one_line(s3, "ceramic_skirting", spec={"window_to_floor": True})
    assert blanket.net_qty == 14.30                      # 18 − 1.00 − 1.20 − 1.50
    assert any("window_to_floor_ids" in b for b in blanket.breakdown)
    print("ok  skirting: doors/arches deducted, windows only when named as French")


# =========================================================================== #
# 5. Waste: net is paid, gross is bought                                       #
# =========================================================================== #
def test_waste_net_vs_gross():
    """net_qty is the interim-certificate quantity; gross_qty is the purchase order.

    ceramic_tile_floor: net 20.20 m², waste 10% -> gross 22.22 m².
    The waste is 2.02 m² of tile bought and never laid, so:
      * labour is priced on the NET (nobody lays offcuts), and
      * paying the client's certificate on the gross would pay the waste twice.
    """
    ln = one_line(room(), "ceramic_tile_floor")
    assert ln.net_qty == 20.20
    assert ln.gross_qty == round(ln.net_qty * (1 + ln.waste_pct), 2) == 22.22
    assert round(ln.gross_qty - ln.net_qty, 2) == 2.02

    priced = price_line(ln, material_rate=10.0, labor_rate=4.0, currency="USD",
                        rate_source="t@1", qty_basis="net")
    assert priced.material_total == 202.00 and priced.labor_total == 80.80

    procurement = price_line(ln, material_rate=10.0, labor_rate=4.0, currency="USD",
                             rate_source="t@1", qty_basis="gross")
    assert procurement.material_total == 222.20      # 22.22 × 10 — the material bought
    assert procurement.labor_total == 80.80          # labour still on the net 20.20


def test_waste_override_lands_in_the_breakdown():
    """An override moves the purchase order, so it is never silent.

    waste 10% -> 25% on ceramic_tile_floor only:
      gross = 20.20 × 1.25 = 25.25 m²
    The narrowest scope wins: a project-wide "*" of 30% is outranked by the
    rule-scoped 25%, and both the old and the new rate are written into the line.
    """
    ln = one_line(room(), "ceramic_tile_floor",
                  overrides={"*": {"waste_pct": 0.30},
                             "ceramic_tile_floor": {"waste_pct": 0.25}})
    assert ln.waste_pct == 0.25
    assert ln.gross_qty == 25.25
    trail = "\n".join(ln.breakdown)
    assert "تعديل يدوي معتمد" in trail and "نسبة الهدر" in trail
    assert "10%" in trail and "25%" in trail            # old value beside the new one

    # An unsupported override name is refused loudly instead of being ignored.
    bogus = one_line(room(), "ceramic_tile_floor",
                     overrides={"ceramic_tile_floor": {"tile_size": "80x80"}})
    assert any("غير مدعوم" in b for b in bogus.breakdown)
    assert bogus.waste_pct == 0.10

    # A spec-driven uplift (herringbone) is data in the rules file, not a hard-code.
    hb = one_line(room(), "ceramic_tile_floor", spec={"laying_pattern": "herringbone"})
    assert hb.waste_pct == 0.15                        # 0.10 + 0.05
    assert hb.gross_qty == 23.23                       # 20.20 × 1.15
    print("ok  waste: net 20.20 / gross 22.22, override 25% audited, herringbone +5%")


# =========================================================================== #
# 6. The safe evaluator — security                                             #
# =========================================================================== #
ENV = {"a": 2.0, "b": 3.0}


def _rejected(formula, env=None):
    with pytest.raises(FormulaError):
        safe_eval(formula, ENV if env is None else env)


def test_evaluator_accepts_valid_arithmetic():
    """Everything a measurement rule legitimately needs, and nothing more."""
    assert safe_eval("2 + 3 * (4 - 1) / 2", {})[0] == 6.5
    assert safe_eval("a * b", ENV)[0] == 6.0
    assert safe_eval("min(a, b) + max(a, b)", ENV)[0] == 5.0
    assert safe_eval("-a + +b", ENV)[0] == 1.0
    assert safe_eval("a - b", ENV)[0] == -1.0
    assert safe_eval("min(a, b, 1.5)", ENV)[0] == 1.5
    assert safe_eval("0.5 * a", ENV)[0] == 1.0
    # the substituted text is the audit trail: it must carry the real numbers
    value, text = safe_eval("a * b - 1", ENV)
    assert value == 5.0 and "2.00" in text and "3.00" in text


def test_evaluator_rejects_attribute_and_dunder_access():
    """Attribute access is the whole escape ladder — it must die at the first rung."""
    for src in (
        "a.real",
        "a.__class__",
        "a.__class__.__mro__",
        "a.__class__.__base__.__subclasses__()",
        "().__class__.__bases__[0].__subclasses__()",
        "a.__dict__",
        "min.__globals__",
        "b.__reduce__",
        "(a).__format__",
        "a . real",
    ):
        _rejected(src)
    # a bare dunder NAME is not in the environment either — no implicit builtins
    for src in ("__import__", "__builtins__", "__class__", "eval", "exec", "open"):
        _rejected(src)
    print("ok  evaluator: attribute + dunder access rejected (10 forms)")


def test_evaluator_rejects_calls_to_non_whitelisted_names():
    """min/max only. Everything else that looks like a call is refused."""
    assert set(ALLOWED_FUNCS) == {"min", "max"}, ALLOWED_FUNCS
    for src in (
        "abs(a)",
        "round(a)",
        "print(a)",
        "eval('1+1')",
        "exec('x=1')",
        "open('/etc/passwd')",
        "__import__('os')",
        "__import__('os').system('echo pwned')",
        "getattr(a, 'real')",
        "globals()",
        "locals()",
        "vars()",
        "compile('1', 'x', 'eval')",
        "type(a)",
        "min(a, b)(a)",
        "(min)(a, b, c=1)",          # keyword arguments are refused outright
        "min(a)",                    # a one-argument min is a mistake, not a formula
        "sum([a, b])",
    ):
        _rejected(src)
    print("ok  evaluator: only min/max may be called (18 forms rejected)")


def test_evaluator_rejects_imports_statements_and_exotic_syntax():
    """A rules file is edited by a non-programmer; it must never become a code path."""
    for src in (
        "import os",
        "from os import system",
        "a = 1",
        "a; b",
        "del a",
        "lambda: 1",
        "(lambda x: x)(a)",
        "[x for x in (1, 2)]",
        "{x for x in (1, 2)}",
        "{'k': a}",
        "(a, b)",
        "[a, b]",
        "a if b else 1",
        "a and b",
        "a or b",
        "not a",
        "a > b",
        "a == b",
        "a[0]",
        "a[0:1]",
        "a ** b",                    # power is not an allowed operator (DoS by size)
        "a % b",
        "a // b",
        "a & b",
        "a | b",
        "a << b",
        "~a",
        "f'{a}'",
        "'abc'",
        "'a' * 10",
        "True",
        "False",
        "None",
        "...",
        "yield a",
        "await a",
        "(x := a)",
    ):
        _rejected(src)
    print("ok  evaluator: imports/statements/comprehensions/comparisons all rejected")


def test_evaluator_refuses_unknown_names_and_unsafe_results():
    """No silent zero for an unknown name, and no non-finite number ever escapes."""
    _rejected("unknown_field")
    _rejected("a + unknown_field")
    _rejected("a / 0")                       # division by zero
    _rejected("a / (b - b)")                 # ... including one that is computed
    _rejected("")                            # an empty formula is not "zero"
    _rejected("   ")
    _rejected("(a + b")                      # syntax error
    _rejected("1 + " * 200 + "1")            # longer than MAX_FORMULA_LEN
    _rejected("+".join(["1"] * 150))         # more nodes than MAX_FORMULA_NODES
    with pytest.raises(FormulaError):
        safe_eval("a", {"a": float("inf")})
    with pytest.raises(FormulaError):
        safe_eval("a", {"a": float("nan")})
    print("ok  evaluator: unknown names, /0, empty, oversized and non-finite refused")


def test_rules_file_itself_is_sound():
    """The shipped rules file must satisfy its own validator, formulas included."""
    assert validate_rules(RULES) == []
    assert len(RULES) >= 50
    for key, rule in RULES.items():
        assert rule["code"] and rule["name_ar"], key
        assert 0.0 <= float(rule["waste_pct"]) <= 1.0, key
    with pytest.raises(RuleError):
        rules_loader.get_rule("no_such_rule_key", RULES)
    # a hand-edited rule that smuggles in an attribute access must fail validation
    for smuggled in ("floor_area.__class__", "floor_area * abs(perimeter_m)",
                     "floor_area if perimeter_m else 0", "[floor_area][0]",
                     "floor_area ** 2"):
        bad = {"x": {**RULES["ceramic_tile_floor"], "formula": smuggled}}
        assert validate_rules(bad), f"validate_rules accepted {smuggled!r}"
    print(f"ok  rules file: {len(RULES)} rules validate clean")


def test_a_poisoned_rules_file_is_refused_at_load_not_at_room_14(tmp_path):
    """A bad formula must kill the FILE, not one line of one room.

    Rejecting it only while evaluating turns it into a zero-quantity warning line
    buried in a 200-line villa — which is a silent under-quote wearing a warning
    label. ``load_rules`` therefore refuses the whole file.
    """
    import yaml

    good = {"rules": {"r1": {
        "code": "TST-OK", "name_ar": "بند اختبار", "surface": "floor", "unit": "m2",
        "formula": "floor_area * 1.0", "deduct_openings": False,
        "deduct_threshold_m2": 0.5, "add_reveals": False, "waste_pct": 0.1,
        "spec_params": {}, "notes_ar": [],
    }}}
    ok_path = tmp_path / "ok.yaml"
    ok_path.write_text(yaml.safe_dump(good, allow_unicode=True), encoding="utf-8")
    assert "r1" in load_rules(str(ok_path), refresh=True)

    poisoned = {"rules": {"r1": {**good["rules"]["r1"],
                                 "formula": "floor_area.__class__.__mro__"}}}
    bad_path = tmp_path / "poisoned.yaml"
    bad_path.write_text(yaml.safe_dump(poisoned, allow_unicode=True), encoding="utf-8")
    with pytest.raises(RuleError):
        load_rules(str(bad_path), refresh=True)
    print("ok  rules file: a poisoned formula is refused at load, not at room 14")


# =========================================================================== #
# 7. Pricing                                                                   #
# =========================================================================== #
def test_price_book_loads_versioned():
    """A book is identified by its own meta, and a quote records id + version."""
    book = load_book("levant")
    assert book.id == "levant-base" and book.region == "levant"
    assert book.currency == "USD"
    assert book.rate_source == f"levant-base@{book.version}"
    assert book.version in pricing.list_versions("levant")
    assert book.fingerprint and len(book.fingerprint) == 16
    assert load_book("levant", book.version).fingerprint == book.fingerprint

    gulf = load_book("gulf")
    assert gulf.currency == "SAR" and gulf.region == "gulf"
    assert "levant" in pricing.regions() and "gulf" in pricing.regions()

    with pytest.raises(BookError):
        load_book("atlantis")
    with pytest.raises(BookError):
        load_book("levant", "99.0.0")
    print(f"ok  price book: levant-base@{book.version} (USD), gulf-base (SAR)")


def test_pricing_one_line_and_the_totals_chain():
    """One line, then the commercial cascade, all by hand.

    Line: ceramic_tile_floor, net 20.20 m², levant-base rates 12.00 / 5.00 USD.
      the book's rates include waste (waste_in_rate: true) -> priced on the NET
      material = 20.20 × 12.00 = 242.40
      labour   = 20.20 ×  5.00 = 101.00
      line total               = 343.40 USD

    Cascade (overhead 10%, profit 10%, VAT 16%, each on the running total):
      subtotal                                = 343.40
      overhead 10% × 343.40 =  34.34  ->  running 377.74
      profit   10% × 377.74 =  37.77  ->  running 415.51
      VAT      16% × 415.51 =  66.48  ->  running 481.99
      grand total                             = 481.99 USD
    Order matters: VAT on top of profit on top of overhead is a different number
    from any other sequence, and this is the Levant contracting convention.
    """
    book = load_book("levant")
    ln = one_line(room(), "ceramic_tile_floor")
    pl = price_one(ln, book)
    assert pl.qty_basis == "net"
    assert pl.material_rate == 12.0 and pl.labor_rate == 5.0
    assert pl.material_total == 242.40
    assert pl.labor_total == 101.00
    assert pl.total == 343.40
    assert pl.rate_source.startswith(book.rate_source)
    assert "placeholder" in pl.rate_source          # the starter book says so on every line

    steps, grand, bd = apply_terms(343.40, QuoteTerms(overhead_pct=0.10, profit_pct=0.10,
                                                      tax_pct=0.16), "USD")
    got = {s.code: (s.amount, s.running_total) for s in steps}
    assert got["overhead"] == (34.34, 377.74)
    assert got["profit"] == (37.77, 415.51)
    assert got["tax"] == (66.48, 481.99)
    assert grand == 481.99
    assert [s.code for s in steps] == ["overhead", "profit", "tax"]
    assert any("481.99" in x for x in bd)

    # A zero percentage produces no step at all — nothing invented, nothing padded.
    steps0, grand0, _ = apply_terms(343.40, QuoteTerms(), "USD")
    assert steps0 == [] and grand0 == 343.40


def test_pricing_unit_mismatch_and_missing_rate_refuse_a_silent_zero():
    book = load_book("levant")
    ln = one_line(room(), "ceramic_skirting")
    assert price_one(ln, book).total == round(17.00 * 2.0 + 17.00 * 1.5, 2)

    from dataclasses import replace as _replace
    wrong_unit = _replace(ln, unit="m2")             # as if a unit_switch had fired
    with pytest.raises(pricing.UnitMismatchError):
        price_one(wrong_unit, book)

    orphan = _replace(ln, rule_key="not_in_any_book")
    with pytest.raises(pricing.RateMissingError):
        price_one(orphan, book)
    assert price_one(orphan, book, on_missing="skip") is None
    zeroed = price_one(orphan, book, on_missing="zero")
    assert zeroed.total == 0.0 and "no-rate" in zeroed.rate_source
    print("ok  pricing: unit mismatch and missing rate both refuse a silent zero")


def test_placeholder_book_blocks_a_sendable_quote():
    """The starter books are placeholders; nothing built on them may be sent."""
    book = load_book("levant")
    assert book.is_placeholder is True

    project = Project(id="P1", name="شقة نموذجية", client_name="أبو محمد",
                      region="levant", currency="USD",
                      spaces=[room(finishes=[FinishAssignment(surface="floor",
                                                              system="ceramic_tile_floor")])])
    lines = takeoff(project, rules=RULES)
    q = quote(project, lines, book, terms=QuoteTerms(overhead_pct=0.10, profit_pct=0.10,
                                                     tax_pct=0.16),
              priced_at="2026-07-26T00:00:00Z")
    assert q.subtotal == 343.40 and q.grand_total == 481.99
    assert q.is_placeholder is True
    assert q.is_client_ready is False
    assert "PLACEHOLDER" in client_block_reason_ar(q) or "أولي" in client_block_reason_ar(q)
    with pytest.raises(PlaceholderRatesError):
        assert_client_ready(q)
    assert any("PLACEHOLDER" in w or "أولي" in w for w in q.warnings_ar)

    # ... and proposal.py must refuse to make it LOOK sendable.
    html = proposal.render_html(project, q.lines, q,
                                options=proposal.ProposalOptions(price_book=book))
    assert "مسودة — غير صالحة للإرسال" in html
    assert html.startswith("<!doctype html>") and 'dir="rtl"' in html
    assert "placeholder" in html.lower() or "مبدئية" in html
    # deterministic: no clock, no randomness — same bytes twice
    assert html == proposal.render_html(project, q.lines, q,
                                        options=proposal.ProposalOptions(price_book=book))
    print("ok  placeholder book: quote blocked + proposal watermarked as draft")


def test_quote_is_reproducible_from_its_own_recipe():
    """book id + version + fingerprint + terms is a complete recipe."""
    book = load_book("levant")
    project = Project(id="P2", name="مشروع", client_name="ع", region="levant",
                      currency="USD",
                      spaces=[room(finishes=[FinishAssignment(surface="wall",
                                                              system="plaster_internal_wall_cement")])])
    lines = takeoff(project, rules=RULES)
    q = quote(project, lines, book, terms=QuoteTerms(overhead_pct=0.12),
              priced_at="2026-07-26T00:00:00Z")
    assert q.book_fingerprint == book.fingerprint
    assert pricing.verify_reproducible(q) == []
    assert pricing.Quote.from_json(q.to_json()).to_dict() == q.to_dict()
    print("ok  quote: reproducible from its recorded book id + version + fingerprint")


# =========================================================================== #
# 8. Validation warnings                                                       #
# =========================================================================== #
def _has(warnings, *needles):
    return any(all(n in w for n in needles) for w in warnings)


def test_validation_zero_dimensions():
    """A zero measures 0 m² without one negative number appearing anywhere.

    That is the single most dangerous silent failure in the product, so the
    schema must say it out loud: zero height, zero wall length, zero floor area.
    """
    s = room(height=0.0)
    w = s.validate()
    assert _has(w, "الارتفاع", "غير صالح")

    s2 = room()
    s2.walls[0].length_m = 0.0
    assert _has(s2.validate(), "W1", "الطول", "غير صالح")

    s3 = room()
    s3.walls[1].height_m = 0.0
    assert _has(s3.validate(), "W2", "الارتفاع", "غير صالح")

    s4 = Space(id="Z", name="غرفة", type="bedroom", height_m=3.0, floor_area_m2=0.0,
               walls=[Wall(id="W1", length_m=4.0, height_m=3.0)])
    assert _has(s4.validate(), "مساحة الأرضية المُدخلة")

    # and the warning must travel WITH the bill, not sit in a method nobody calls
    lines = takeoff_space(room(height=0.0, finishes=[
        FinishAssignment(surface="wall", system="plaster_internal_wall_cement")]),
        rules=RULES)
    assert any("الارتفاع" in x for x in warnings_of(lines))


def test_validation_opening_bigger_than_its_wall():
    """A 3.00 m opening in a 2.00 m wall is a drawing error, not a quantity."""
    s = room()
    s.walls[0].length_m = 2.00
    s.walls[0].openings = [_door(w=3.00, h=2.10)]
    w = s.validate()
    assert _has(w, "عرض الفتحة", "أكبر من طول")

    s2 = room()
    s2.walls[0].openings = [_door(h=3.50)]              # taller than the 3.00 m wall
    assert _has(s2.validate(), "ارتفاع الفتحة", "أكبر من")

    s3 = room()
    s3.walls[3].openings = [_window(w=3.50, h=2.80)]    # 9.80 m² in a 12.00 m² wall...
    s3.walls[3].openings.append(_door(w=1.00, h=2.50))  # ... plus 2.50 = 12.30 m²
    assert _has(s3.validate(), "يتجاوز مساحة الجدار")


def test_validation_absurd_ceiling_height():
    """2.00–6.00 m is the sane band; outside it the surveyor must look."""
    assert _has(room(height=9.0).validate(), "خارج المدى المعقول")
    assert _has(room(height=1.2).validate(), "خارج المدى المعقول")
    assert room(height=2.8).validate() == []            # a normal room warns about nothing

    # unknown space type / unknown surface are flagged too
    assert _has(room(type_="ballroom").validate(), "نوع الفراغ")
    odd = room()
    odd.finishes = [FinishAssignment(surface="roof", system="ceramic_tile_floor")]
    assert _has(odd.validate(), "غير معروف")

    # polygon that disagrees with the typed area by more than 5%
    off = room(floor_area=30.0)                         # polygon says 20.00 m²
    assert _has(off.validate(), "تختلف عن المساحة")
    # an unsnapped vertex: a 0.20 m gap between first and last point
    gap = room(polygon=ROOM_POLY + [(0.0, 0.20)])
    assert _has(gap.validate(), "شبه مغلق")
    print("ok  validation: zero dims, oversized openings, absurd heights all warn")


def test_unknown_finish_becomes_a_visible_hole_not_a_missing_line():
    """A skipped item must be a zero-quantity WARNING line, never an absence."""
    s = room(finishes=[FinishAssignment(surface="floor", system="unobtainium_floor")])
    lines = takeoff_space(s, rules=RULES)
    assert len(lines) == 1 and is_warning(lines[0])
    assert lines[0].net_qty == 0.0
    assert any("غير معروف" in b for b in lines[0].breakdown)
    assert summarize(lines)["totals"]["warnings"] == 1
    print("ok  unknown finish: one visible zero-quantity warning line")


# =========================================================================== #
# 9. Round-trip                                                                #
# =========================================================================== #
def test_project_round_trip_preserves_everything():
    """to_dict -> from_dict -> to_dict is the wire format every adapter speaks.

    DXF, PDF, image and manual entry all produce this one dict, so anything lost
    in the round-trip is lost for every ingestion path at once.
    """
    p = Project(
        id="P9", name="فيلا الرابية", client_name="م. سامي", region="levant",
        currency="JOD", created_at="2026-01-02T03:04:05Z",
        spaces=[
            room(finishes=[
                FinishAssignment(surface="floor", system="ceramic_tile_floor",
                                 spec={"tile_size": "80x80", "laying_pattern": "diagonal",
                                       "deduct_areas_m2": [0.8, 0.3]}),
                FinishAssignment(surface="wall", system="plaster_internal_wall_cement",
                                 spec={"reveal_width_m": 0.18}),
            ]),
            Space(id="S2", name="حمّام", type="bath", height_m=2.70,
                  floor_area_m2=4.5, polygon=None,
                  walls=[Wall(id="B1", length_m=2.5, height_m=2.70, is_external=True,
                              openings=[_door("D2", 0.8, 2.0),
                                        Opening(id="K2", kind="niche", width_m=0.4,
                                                height_m=0.6, count=2, wall_id="B1")]),
                         Wall(id="B2", length_m=1.8, height_m=2.70)],
                  finishes=[FinishAssignment(surface="wall", system="wall_tile_ceramic_wet",
                                             spec={"tile_height_m": 2.2})]),
        ],
    )
    back = Project.from_dict(p.to_dict())
    assert back.to_dict() == p.to_dict()
    assert Project.from_json(p.to_json()).to_dict() == p.to_dict()

    # field-by-field, not just dict equality
    assert back.created_at == "2026-01-02T03:04:05Z"
    assert back.currency == "JOD" and back.region == "levant"
    s1, s2 = back.spaces
    assert s1.polygon == [(0.0, 0.0), (5.0, 0.0), (5.0, 4.0), (0.0, 4.0)]
    assert s1.floor_area == 20.0 and s1.perimeter_m == 18.0
    assert s1.finishes[0].spec["deduct_areas_m2"] == [0.8, 0.3]
    assert s1.finishes[1].spec["reveal_width_m"] == 0.18
    assert s2.polygon is None and s2.floor_area_m2 == 4.5
    assert s2.walls[0].is_external is True
    assert s2.walls[0].openings[1].kind == "niche" and s2.walls[0].openings[1].count == 2
    assert s2.walls[0].openings[1].wall_id == "B1"

    # the only test that really matters: the same bill comes out the other side
    before = takeoff(p, rules=RULES)
    after = takeoff(back, rules=RULES)
    assert [(l.code, l.space_id, l.net_qty, l.waste_pct, l.gross_qty) for l in before] == \
           [(l.code, l.space_id, l.net_qty, l.waste_pct, l.gross_qty) for l in after]
    assert [l.breakdown for l in before] == [l.breakdown for l in after]
    print("ok  round-trip: dict/JSON identical, and the bill re-measures identically")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL TESTS PASSED.")


if __name__ == "__main__":
    main()
