# -*- coding: utf-8 -*-
"""Persistence — one SQLite file, one row per project, one serialisation format.

The core already owns a lossless, tested round-trip: ``Project.to_dict()`` /
``Project.from_dict()``. This module stores exactly that dict as JSON in a ``doc``
column and nothing else. There is deliberately NO ORM and NO second schema:

* a normalised spaces/walls/openings table set would be a SECOND definition of the
  data contract, and the day it drifts from ``core.schema`` the drift shows up as a
  wrong quantity in a client's bill — the single failure mode this product exists to
  prevent;
* the columns beside ``doc`` (name, client_name, region, currency) are a denormalised
  INDEX for the projects list, never the source of truth. They are rewritten from the
  document on every save, so they can never disagree with it.

Every connection is opened per call: FastAPI runs sync endpoints on a thread pool and
a SQLite connection may not be shared across threads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from core.schema import Project

__all__ = ["DB_PATH", "init_db", "list_projects", "create_project", "get_project",
           "get_row", "save_project", "delete_project", "project_exists", "StorageError"]

# <repo>/data/engeneer.db — created on first run, alongside core/ and api/.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "engeneer.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    client_name TEXT NOT NULL DEFAULT '',
    region      TEXT NOT NULL DEFAULT '',
    currency    TEXT NOT NULL DEFAULT '',
    doc         TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);
"""


class StorageError(RuntimeError):
    """The database could not be read or written."""


def _now() -> str:
    """UTC, to the millisecond.

    The millisecond is not cosmetic: ``updated_at`` is the version token the PUT
    precondition compares, and an autosave debounce is 1.2 s. At one-second
    resolution two saves inside the same second are indistinguishable, so a genuine
    conflict between them reads as "same version" and is applied instead of refused.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _db_path() -> str:
    return os.environ.get("ENGENEER_DB", DB_PATH)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> str:
    """Create the file and the table if they are not there yet. Idempotent."""
    with connect() as conn:
        conn.executescript(SCHEMA)
    return _db_path()


# --------------------------------------------------------------------------- #
# Reads                                                                         #
# --------------------------------------------------------------------------- #
def _space_count(doc_json: str) -> int:
    try:
        return len((json.loads(doc_json) or {}).get("spaces") or [])
    except (ValueError, TypeError):
        return 0


def list_projects() -> list[dict]:
    """The index the projects screen shows. Newest edit first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, client_name, region, currency, created_at, updated_at, doc "
            "FROM projects ORDER BY updated_at DESC, created_at DESC"
        ).fetchall()
    return [{
        "id": r["id"],
        "name": r["name"],
        "client_name": r["client_name"],
        "region": r["region"],
        "currency": r["currency"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "space_count": _space_count(r["doc"]),
    } for r in rows]


def get_row(project_id: str) -> Optional[dict]:
    with connect() as conn:
        r = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(r) if r is not None else None


def get_project(project_id: str) -> Optional[tuple[Project, dict]]:
    """``(Project, row)`` or ``None``. The Project comes back through the core's
    own ``from_dict``, so nothing about it is re-interpreted here."""
    row = get_row(project_id)
    if row is None:
        return None
    try:
        doc = json.loads(row["doc"])
    except ValueError as exc:
        raise StorageError(f"project {project_id!r}: stored document is not valid JSON") from exc
    return Project.from_dict(doc), row


def project_exists(project_id: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM projects WHERE id = ?",
                            (project_id,)).fetchone() is not None


# --------------------------------------------------------------------------- #
# Writes                                                                        #
# --------------------------------------------------------------------------- #
def _dump(project: Project) -> str:
    return json.dumps(project.to_dict(), ensure_ascii=False, sort_keys=True)


def create_project(name: str, client_name: str = "", region: str = "",
                   currency: str = "") -> str:
    """Insert an empty project and return its id."""
    pid = uuid.uuid4().hex[:12]
    project = Project(id=pid, name=str(name), client_name=str(client_name),
                      region=str(region), currency=str(currency), spaces=[])
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, client_name, region, currency, doc, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, project.name, project.client_name, project.region, project.currency,
             _dump(project), project.created_at or now, now),
        )
    return pid


def save_project(project_id: str, project: Project) -> Optional[str]:
    """Overwrite a project with a new document. Returns the new ``updated_at``,
    or ``None`` if the id does not exist.

    The row's id ALWAYS wins over the id inside the payload: a client that posts a
    document carrying someone else's id must not be able to overwrite that project,
    and must not be able to orphan its own.
    """
    row = get_row(project_id)
    if row is None:
        return None
    doc = project.to_dict()
    doc["id"] = project_id
    doc["created_at"] = doc.get("created_at") or row["created_at"]
    now = _now()
    with connect() as conn:
        conn.execute(
            "UPDATE projects SET name=?, client_name=?, region=?, currency=?, doc=?, "
            "updated_at=? WHERE id=?",
            (str(doc.get("name") or ""), str(doc.get("client_name") or ""),
             str(doc.get("region") or ""), str(doc.get("currency") or ""),
             json.dumps(doc, ensure_ascii=False, sort_keys=True), now, project_id),
        )
    return now


def delete_project(project_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cur.rowcount > 0
