# -*- coding: utf-8 -*-
"""A worked example: one 5.00 x 4.00 m salon, floor tiles, from geometry to a priced line.

Run from the repository root:

    python examples/demo.py

It prints the Arabic audit trail the engine attaches to every quantity. That trail
is the product feature, not debug output -- it is what lets a quantity surveyor
argue the number with a contractor line by line.
"""

import os
import sys

# Windows consoles default to a legacy codepage that cannot render Arabic.
# The bill is the deliverable, so force UTF-8 rather than mangling it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.boq_engine import takeoff, warnings_of
from core.rules_loader import load_rules
from core.schema import FinishAssignment, Opening, Project, Space, Wall

RULES = load_rules()

# A plain salon: 5.00 x 4.00 m, clear height 3.00 m, one door and one window.
salon = Space(
    id="S1",
    name="صالة",
    type="living",
    height_m=3.00,
    polygon=[(0.0, 0.0), (5.0, 0.0), (5.0, 4.0), (0.0, 4.0)],
    walls=[
        Wall(id="W1", length_m=5.00, height_m=3.00,
             openings=[Opening(id="D1", kind="door", width_m=1.00, height_m=2.10)]),
        Wall(id="W2", length_m=4.00, height_m=3.00,
             openings=[Opening(id="N1", kind="window", width_m=1.20, height_m=1.40)]),
        Wall(id="W3", length_m=5.00, height_m=3.00),
        Wall(id="W4", length_m=4.00, height_m=3.00),
    ],
    finishes=[
        FinishAssignment(surface="floor", system="ceramic_tile_floor"),
        FinishAssignment(surface="wall", system="paint_emulsion_internal_wall"),
    ],
)

project = Project(
    id="demo",
    name="مشروع تجريبي",
    client_name="عميل تجريبي",
    region="levant",
    currency="USD",
    spaces=[salon],
)

lines = takeoff(project, rules=RULES)

print("=" * 72)
print(f"BOQ lines produced: {len(lines)}")
print("=" * 72)

for line in lines:
    print()
    print(f"[{line.code}] {line.name_ar}")
    print(f"  net {line.net_qty} {line.unit}  ->  gross {line.gross_qty} {line.unit}")
    print("  --- الاحتساب ---")
    for entry in line.breakdown:
        print(f"  {entry}")

warns = warnings_of(lines)
print()
print("=" * 72)
print(f"warnings: {len(warns)}")
for w in warns:
    print(f"  ! {w}")
