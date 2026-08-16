"""Deterministic Arabic BOQ core — geometry contract, finish rules, safe evaluator.

Nothing in this package calls an LLM. Every quantity is arithmetic over the
dataclasses in :mod:`core.schema`, driven by the data in ``core/rules/*.yaml``,
and every number carries an Arabic ``breakdown`` audit trail back to its own
arithmetic. A wrong quantity is real money lost and real legal liability, so the
core stays reproducible from the JSON alone.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .schema import (  # noqa: F401
    BOQLine,
    FinishAssignment,
    GeometryError,
    Opening,
    PricedLine,
    Project,
    Space,
    Wall,
    price_line,
)
from .boq_engine import (  # noqa: F401
    is_warning,
    summarize,
    takeoff,
    takeoff_space,
    warnings_of,
)

__all__ = [
    "BOQLine", "FinishAssignment", "GeometryError", "Opening", "PricedLine",
    "Project", "Space", "Wall", "price_line",
    "takeoff", "takeoff_space", "summarize", "is_warning", "warnings_of",
    "__version__",
]
