"""SQLite file for the response cache and research jobs."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import threading
import time
from typing import Any, Dict, Optional


class Store:
    def __init__(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, created REAL NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, value TEXT NOT NULL);
                """
            )

    @staticmethod
    def key(kind: str, payload: Dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, default=str)
        return kind + ":" + hashlib.sha256(blob.encode()).hexdigest()

    def cached(self, key: str, max_age_hours: Optional[float]) -> Optional[Any]:
        """max_age_hours None/0 means skip the cache; -1 means any age."""
        if not max_age_hours:
            return None
        with self._lock:
            row = self._db.execute("SELECT created, value FROM cache WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if max_age_hours > 0 and time.time() - row[0] > max_age_hours * 3600:
            return None
        return json.loads(row[1])

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cache(key, created, value) VALUES (?,?,?)",
                (key, time.time(), json.dumps(value)),
            )
            self._db.commit()

    def save_job(self, job: Dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO research(id, created, value) VALUES (?,?,?)",
                (job["request_id"], time.time(), json.dumps(job)),
            )
            self._db.commit()

    def job(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT value FROM research WHERE id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None
