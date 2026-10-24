"""Local dictation history in a SQLite file next to settings.json."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class History:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        # Used from the dictation worker and the tray menu; one connection, one user at a time.
        self.lock = threading.Lock()
        try:
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY,
                    created REAL NOT NULL,
                    app TEXT,
                    app_type TEXT,
                    seconds REAL,
                    raw_text TEXT,
                    final_text TEXT,
                    latency REAL
                )"""
            )
            self.db.commit()
        except sqlite3.DatabaseError:
            self.db.close()  # a damaged file: let go of it so it can be moved aside
            raise

    def add(self, app: str, app_type: str, seconds: float, raw: str, final: str, latency: float) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO history (created, app, app_type, seconds, raw_text, final_text, latency)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), app, app_type, seconds, raw, final, latency),
            )
            self.db.commit()

    def last(self) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT final_text FROM history ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def recent(self, n: int = 20) -> list[tuple]:
        with self.lock:
            return self.db.execute(
                "SELECT created, app, final_text FROM history ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
