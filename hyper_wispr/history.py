"""Local dictation history, notes and word suggestions in a SQLite file next to settings.json.

Dictated text can contain patient information, so every piece of text (and any kept recording)
is encrypted with the Windows account's own key before it is written (see secure.py).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import secure

DAY = 86400


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def word_count(text: str) -> int:
    return len(text.split())


class History:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        # Used from the dictation worker, the tray menu and the Settings window; one at a time.
        self.lock = threading.Lock()
        # Decrypted texts by (id, time), so a search doesn't decrypt every dictation each time
        # (1.5 s for 3,000 of them on Windows). Only the Settings window searches.
        self._plain: dict[tuple[int, float], tuple[str, str]] = {}
        try:
            # What's deleted is overwritten with zeros, so deleted text doesn't linger in the file.
            self.db.execute("PRAGMA secure_delete = ON")
            self.db.executescript(
                """CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY,
                    created REAL NOT NULL,
                    app TEXT,
                    app_type TEXT,
                    seconds REAL,
                    raw_text TEXT,
                    final_text TEXT,
                    latency REAL
                );
                -- Recordings kept for "Redo"; only the last few, deleted with their dictation.
                CREATE TABLE IF NOT EXISTS recordings (
                    history_id INTEGER PRIMARY KEY,
                    created REAL NOT NULL,
                    audio BLOB
                );
                -- Totals per day, so usage stats survive deleting old history.
                CREATE TABLE IF NOT EXISTS daily (
                    day TEXT PRIMARY KEY,
                    dictations INTEGER NOT NULL DEFAULT 0,
                    words INTEGER NOT NULL DEFAULT 0,
                    seconds REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    body BLOB
                );
                -- Words you corrected after a dictation, offered for the dictionary.
                CREATE TABLE IF NOT EXISTS suggestions (
                    word TEXT PRIMARY KEY,
                    heard TEXT,
                    count INTEGER NOT NULL DEFAULT 1,
                    updated REAL NOT NULL,
                    dismissed INTEGER NOT NULL DEFAULT 0
                );"""
            )
            self.db.commit()
            self._encrypt_old_rows()
            self._backfill_daily()
        except sqlite3.DatabaseError:
            self.db.close()  # a damaged file: let go of it so it can be moved aside
            raise

    def _encrypt_old_rows(self) -> None:
        """Versions before 0.4 stored text as is; encrypt it once."""
        if not secure.available():
            return
        with self.lock:
            rows = self.db.execute(
                "SELECT id, raw_text, final_text FROM history WHERE typeof(final_text) = 'text' OR typeof(raw_text) = 'text'"
            ).fetchall()
            for row_id, raw, final in rows:
                self.db.execute(
                    "UPDATE history SET raw_text = ?, final_text = ? WHERE id = ?",
                    (secure.encrypt_text(secure.decrypt_text(raw)), secure.encrypt_text(secure.decrypt_text(final)), row_id),
                )
            if rows:
                self.db.commit()

    def _backfill_daily(self) -> None:
        """Daily totals start in 0.4; work them out once from the history kept before that."""
        with self.lock:
            if self.db.execute("SELECT 1 FROM daily LIMIT 1").fetchone():
                return
            rows = self.db.execute("SELECT created, seconds, final_text FROM history").fetchall()
            for created, seconds, final in rows:
                text = secure.decrypt_text(final)
                if text.strip():
                    self._count(created, seconds, text)
            self.db.commit()

    # ---- dictations --------------------------------------------------------
    def add(self, app: str, app_type: str, seconds: float, raw: str, final: str, latency: float,
            audio: np.ndarray | None = None, keep_recordings: int = 0) -> int:
        now = time.time()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO history (created, app, app_type, seconds, raw_text, final_text, latency)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, app, app_type, seconds, secure.encrypt_text(raw), secure.encrypt_text(final), latency),
            )
            row_id = int(cur.lastrowid)
            if final.strip():
                self._count(now, seconds, final)
            if audio is not None and keep_recordings > 0:
                pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
                self.db.execute("INSERT OR REPLACE INTO recordings VALUES (?, ?, ?)", (row_id, now, secure.encrypt(pcm)))
                self.db.execute(
                    "DELETE FROM recordings WHERE history_id NOT IN"
                    " (SELECT history_id FROM recordings ORDER BY history_id DESC LIMIT ?)",
                    (keep_recordings,),
                )
            self.db.commit()
        return row_id

    def _count(self, when: float, seconds: float, final: str) -> None:
        self.db.execute(
            "INSERT INTO daily (day, dictations, words, seconds) VALUES (?, 1, ?, ?)"
            " ON CONFLICT(day) DO UPDATE SET dictations = dictations + 1, words = words + excluded.words,"
            " seconds = seconds + excluded.seconds",
            (_day(when), word_count(final), seconds or 0.0),
        )

    def count_usage(self, seconds: float, final: str, when: float | None = None) -> None:
        """Count a dictation that isn't saved (history switched off) in the daily totals.
        add() counts the ones it saves."""
        with self.lock:
            self._count(when or time.time(), seconds, final)
            self.db.commit()

    def daily(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT day, dictations, words, seconds FROM daily ORDER BY day").fetchall()
        return [{"day": d, "dictations": n, "words": w, "seconds": s} for d, n, w, s in rows]

    def last(self) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT final_text FROM history ORDER BY id DESC LIMIT 1").fetchone()
        return secure.decrypt_text(row[0]) if row else None

    def recent(self, n: int = 200, search: str = "") -> list[dict[str, Any]]:
        """Newest first. search: only dictations containing these words (any case). The text is
        encrypted, so the search runs here rather than in SQL."""
        with self.lock:
            rows = self.db.execute(
                "SELECT h.id, h.created, h.app, h.app_type, h.seconds, h.raw_text, h.final_text,"
                " r.history_id IS NOT NULL FROM history h LEFT JOIN recordings r ON r.history_id = h.id"
                " ORDER BY h.id DESC"
            ).fetchall()
        needles = search.lower().split()
        out = []
        for row_id, created, app, app_type, seconds, raw, final, has_audio in rows:
            key = (row_id, created)
            if key not in self._plain:
                self._plain[key] = (secure.decrypt_text(final), secure.decrypt_text(raw))
            text, raw_text = self._plain[key]
            if needles and not all(w in text.lower() or w in raw_text.lower() for w in needles):
                continue
            out.append({
                "id": row_id, "created": created, "app": app or "", "app_type": app_type or "",
                "seconds": seconds or 0, "text": text, "raw": raw_text, "can_redo": bool(has_audio),
            })
            if len(out) >= n:
                break
        return out

    def get(self, row_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT app, app_type, seconds, raw_text, final_text FROM history WHERE id = ?", (int(row_id),)
            ).fetchone()
        if not row:
            return None
        app, app_type, seconds, raw, final = row
        return {"id": row_id, "app": app or "", "app_type": app_type or "", "seconds": seconds or 0,
                "raw": secure.decrypt_text(raw), "text": secure.decrypt_text(final)}

    def recording(self, row_id: int) -> np.ndarray | None:
        with self.lock:
            row = self.db.execute("SELECT audio FROM recordings WHERE history_id = ?", (int(row_id),)).fetchone()
        if not row or row[0] is None:
            return None
        return np.frombuffer(secure.decrypt(row[0]), dtype="<i2").astype(np.float32) / 32767

    def update_text(self, row_id: int, final: str) -> None:
        with self.lock:
            self.db.execute("UPDATE history SET final_text = ? WHERE id = ?", (secure.encrypt_text(final), int(row_id)))
            self.db.commit()
            self._plain = {k: v for k, v in self._plain.items() if k[0] != int(row_id)}

    def delete(self, row_id: int | None = None) -> None:
        """One dictation, or all of them (row_id None)."""
        with self.lock:
            if row_id is None:
                self.db.execute("DELETE FROM history")
                self.db.execute("DELETE FROM recordings")
            else:
                self.db.execute("DELETE FROM history WHERE id = ?", (int(row_id),))
                self.db.execute("DELETE FROM recordings WHERE history_id = ?", (int(row_id),))
            self.db.commit()
            self._plain = {k: v for k, v in self._plain.items() if row_id is not None and k[0] != int(row_id)}
            if row_id is None:
                self.db.execute("VACUUM")  # gives the space back; secure_delete has already wiped it

    def delete_older_than(self, days: float, now: float | None = None) -> int:
        """Auto-delete: remove dictations older than this many days. Returns how many went."""
        if not days or days <= 0:
            return 0
        cutoff = (now or time.time()) - days * DAY
        with self.lock:
            n = self.db.execute("DELETE FROM history WHERE created < ?", (cutoff,)).rowcount
            self.db.execute("DELETE FROM recordings WHERE created < ? OR history_id NOT IN (SELECT id FROM history)", (cutoff,))
            self.db.commit()
            if n:
                self.db.execute("VACUUM")  # gives the space back; secure_delete has already wiped it
        return n

    # ---- notes -------------------------------------------------------------
    def notes(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT id, created, updated, body FROM notes ORDER BY updated DESC").fetchall()
        return [{"id": i, "created": c, "updated": u, "body": secure.decrypt_text(b)} for i, c, u, b in rows]

    def save_note(self, body: str, note_id: int | None = None) -> int:
        now = time.time()
        with self.lock:
            if note_id is None:
                cur = self.db.execute("INSERT INTO notes (created, updated, body) VALUES (?, ?, ?)", (now, now, secure.encrypt_text(body)))
                note_id = int(cur.lastrowid)
            else:
                self.db.execute("UPDATE notes SET updated = ?, body = ? WHERE id = ?", (now, secure.encrypt_text(body), int(note_id)))
            self.db.commit()
        return note_id

    def delete_note(self, note_id: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
            self.db.commit()

    # ---- learned words -----------------------------------------------------
    def suggest(self, word: str, heard: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO suggestions (word, heard, updated) VALUES (?, ?, ?)"
                " ON CONFLICT(word) DO UPDATE SET count = count + 1, heard = excluded.heard, updated = excluded.updated",
                (word, heard, time.time()),
            )
            self.db.commit()

    def suggestions(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT word, heard, count FROM suggestions WHERE dismissed = 0 ORDER BY count DESC, updated DESC"
            ).fetchall()
        return [{"word": w, "heard": h, "count": c} for w, h, c in rows]

    def dismiss_suggestion(self, word: str) -> None:
        """Added to the dictionary or turned down: either way, don't offer it again."""
        with self.lock:
            self.db.execute("UPDATE suggestions SET dismissed = 1 WHERE word = ?", (word,))
            self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()


def open_default() -> History:
    from .config import app_dir

    return History(app_dir() / "history.sqlite3")
