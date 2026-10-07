from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ZERO_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class StoredEvent:
    sequence: int
    event_id: str
    event_type: str
    occurred_at: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class EventStore:
    """Small append-only SQLite event log with idempotent IDs and a hash chain."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL
            )
        """)
        self._db.commit()
        self.verify()

    def append(self, event_id: str, event_type: str, payload: dict[str, Any],
               occurred_at: datetime | None = None) -> StoredEvent:
        if not event_id or not event_type:
            raise ValueError("event_id and event_type are required")
        at = (occurred_at or datetime.now(timezone.utc)).isoformat()
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        existing = self._db.execute(
            "SELECT sequence,event_id,event_type,occurred_at,payload_json,previous_hash,event_hash FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing:
            if existing[2] != event_type or existing[4] != body:
                raise ValueError("event_id reused with different event content")
            return self._row_to_event(existing)
        previous = self._db.execute("SELECT event_hash FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
        prev_hash = previous[0] if previous else ZERO_HASH
        event_hash = self._hash(prev_hash, event_id, event_type, at, body)
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO events(event_id,event_type,occurred_at,payload_json,previous_hash,event_hash) VALUES(?,?,?,?,?,?)",
                (event_id, event_type, at, body, prev_hash, event_hash),
            )
        return StoredEvent(cursor.lastrowid, event_id, event_type, at, payload, prev_hash, event_hash)

    def events(self) -> list[StoredEvent]:
        rows = self._db.execute(
            "SELECT sequence,event_id,event_type,occurred_at,payload_json,previous_hash,event_hash FROM events ORDER BY sequence"
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def verify(self) -> bool:
        previous = ZERO_HASH
        for event in self.events():
            body = json.dumps(event.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected = self._hash(previous, event.event_id, event.event_type, event.occurred_at, body)
            if event.previous_hash != previous or event.event_hash != expected:
                raise ValueError(f"event log integrity check failed at sequence {event.sequence}")
            previous = event.event_hash
        return True

    @staticmethod
    def _hash(previous: str, event_id: str, event_type: str, at: str, body: str) -> str:
        material = "\0".join((previous, event_id, event_type, at, body)).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _row_to_event(row: tuple[Any, ...]) -> StoredEvent:
        return StoredEvent(row[0], row[1], row[2], row[3], json.loads(row[4]), row[5], row[6])

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
