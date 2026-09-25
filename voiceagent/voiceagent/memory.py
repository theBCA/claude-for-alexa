"""Local SQLite memory: short-term conversation turns and long-term facts.

Short-term history expires after a few idle hours so every morning starts fresh,
while facts ("my partner is Ceren", "I like the lights warm") persist forever
until the user asks to forget them.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path


class Memory:
    def __init__(self, path: str):
        p = Path(os.path.expanduser(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(p), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL,
                    role TEXT NOT NULL, text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL, text TEXT NOT NULL);
                """
            )
            self._db.commit()

    # conversation -----------------------------------------------------------
    def add_turn(self, role: str, text: str) -> None:
        if not text.strip():
            return
        with self._lock:
            self._db.execute("INSERT INTO turns (ts, role, text) VALUES (?, ?, ?)", (time.time(), role, text))
            self._db.commit()

    def recent_messages(self, max_turns: int, max_age_hours: float) -> list[dict]:
        cutoff = time.time() - max_age_hours * 3600
        with self._lock:
            rows = self._db.execute(
                "SELECT role, text FROM turns WHERE ts >= ? ORDER BY id DESC LIMIT ?", (cutoff, max_turns)
            ).fetchall()
        rows.reverse()
        # The API needs strict user/assistant alternation starting with user.
        msgs: list[dict] = []
        for role, text in rows:
            if not msgs and role != "user":
                continue
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + text
            else:
                msgs.append({"role": role, "content": text})
        return msgs

    # long-term facts ----------------------------------------------------------
    def add_fact(self, text: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO facts (ts, text) VALUES (?, ?)", (time.time(), text.strip()))
            self._db.commit()

    def remove_facts(self, contains: str) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM facts WHERE text LIKE ?", (f"%{contains}%",))
            self._db.commit()
            return cur.rowcount

    def remove_fact(self, text: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM facts WHERE text = ?", (text,))
            self._db.commit()
            return cur.rowcount > 0

    def facts(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT text FROM facts ORDER BY id").fetchall()]
