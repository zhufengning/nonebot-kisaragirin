from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(slots=True)
class ShortTermMessage:
    role: str
    content: str
    created_at: float


class SQLiteMemoryStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS long_term_memory (
                    conversation_id TEXT PRIMARY KEY,
                    memory TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS short_term_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_short_term_conv_time
                ON short_term_memory (conversation_id, created_at)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_description_cache (
                    image_sha256 TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS url_summary_cache (
                    url TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS short_term_image_refs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    user_created_at REAL NOT NULL,
                    image_index INTEGER NOT NULL,
                    image_sha256 TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_short_term_image_refs_conv_time
                ON short_term_image_refs (conversation_id, user_created_at)
                """
            )
            self._conn.commit()

    def get_long_term(self, conversation_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT memory FROM long_term_memory WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return ""
        return str(row["memory"])

    def set_long_term(self, conversation_id: str, memory: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO long_term_memory (conversation_id, memory, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id)
                DO UPDATE SET memory = excluded.memory, updated_at = excluded.updated_at
                """,
                (conversation_id, memory, now),
            )
            self._conn.commit()

    def append_short_term(self, conversation_id: str, role: str, content: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO short_term_memory (conversation_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (conversation_id, role, content, now),
            )
            self._conn.commit()

    def get_short_term(self, conversation_id: str, turn_window: int) -> list[ShortTermMessage]:
        limit = max(1, turn_window) * 2
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, role, content, created_at
                FROM short_term_memory
                WHERE conversation_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()

        messages = [
            ShortTermMessage(
                role=str(r["role"]),
                content=str(r["content"]),
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]
        messages.reverse()
        return messages

    def format_short_term_context(self, conversation_id: str, turn_window: int) -> str:
        messages = self.get_short_term(conversation_id, turn_window=turn_window)
        if not messages:
            return "(empty)"
        lines = []
        for idx, item in enumerate(messages, start=1):
            lines.append(f"{idx}. [{item.role}] {item.content}")
        return "\n".join(lines)

    def persist_turn(
        self,
        conversation_id: str,
        long_term_memory: str,
        user_message: str,
        assistant_reply: str,
        user_image_hashes: Sequence[str] | None = None,
    ) -> None:
        conversation_id = str(conversation_id)
        long_term_memory = str(long_term_memory)
        user_message = str(user_message)
        assistant_reply = str(assistant_reply)
        now = time.time()
        assistant_time = now + 1e-6
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO long_term_memory (conversation_id, memory, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(conversation_id)
                    DO UPDATE SET memory = excluded.memory, updated_at = excluded.updated_at
                    """,
                    (conversation_id, long_term_memory, now),
                )
                self._conn.execute(
                    """
                    INSERT INTO short_term_memory (conversation_id, role, content, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (conversation_id, "user", user_message, now),
                )
                self._conn.execute(
                    """
                    INSERT INTO short_term_memory (conversation_id, role, content, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (conversation_id, "assistant", assistant_reply, assistant_time),
                )
                for image_index, image_hash in enumerate(user_image_hashes or (), start=1):
                    normalized_hash = str(image_hash).strip().lower()
                    if not normalized_hash:
                        continue
                    self._conn.execute(
                        """
                        INSERT INTO short_term_image_refs (
                            conversation_id,
                            user_created_at,
                            image_index,
                            image_sha256
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (conversation_id, now, image_index, normalized_hash),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def get_short_term_image_hashes(self, conversation_id: str, turn_window: int) -> list[str]:
        turn_limit = max(1, turn_window)
        with self._lock:
            turn_rows = self._conn.execute(
                """
                SELECT created_at
                FROM short_term_memory
                WHERE conversation_id = ? AND role = 'user'
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (conversation_id, turn_limit),
            ).fetchall()

            if not turn_rows:
                return []

            turn_times = [float(row["created_at"]) for row in turn_rows]
            placeholders = ",".join("?" for _ in turn_times)
            rows = self._conn.execute(
                f"""
                SELECT id, user_created_at, image_index, image_sha256
                FROM short_term_image_refs
                WHERE conversation_id = ?
                  AND user_created_at IN ({placeholders})
                ORDER BY user_created_at ASC, image_index ASC, id ASC
                """,
                (conversation_id, *turn_times),
            ).fetchall()

        if not rows:
            return []
        hashes: list[str] = []
        seen_hashes: set[str] = set()
        for row in rows:
            value = str(row["image_sha256"]).strip().lower()
            if not value or value in seen_hashes:
                continue
            seen_hashes.add(value)
            hashes.append(value)
        return hashes

    def get_short_term_image_refs(
        self, conversation_id: str, turn_window: int
    ) -> dict[float, dict[int, str]]:
        turn_limit = max(1, turn_window)
        with self._lock:
            turn_rows = self._conn.execute(
                """
                SELECT created_at
                FROM short_term_memory
                WHERE conversation_id = ? AND role = 'user'
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (conversation_id, turn_limit),
            ).fetchall()
            if not turn_rows:
                return {}

            turn_times = [float(row["created_at"]) for row in turn_rows]
            placeholders = ",".join("?" for _ in turn_times)
            rows = self._conn.execute(
                f"""
                SELECT user_created_at, image_index, image_sha256
                FROM short_term_image_refs
                WHERE conversation_id = ?
                  AND user_created_at IN ({placeholders})
                ORDER BY user_created_at ASC, image_index ASC, id ASC
                """,
                (conversation_id, *turn_times),
            ).fetchall()

        refs: dict[float, dict[int, str]] = {}
        for row in rows:
            turn_time = float(row["user_created_at"])
            image_index = int(row["image_index"])
            image_hash = str(row["image_sha256"]).strip().lower()
            if not image_hash:
                continue
            refs.setdefault(turn_time, {})[image_index] = image_hash
        return refs

    def clear_conversation(self, conversation_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM short_term_memory WHERE conversation_id = ?",
                (conversation_id,),
            )
            self._conn.execute(
                "DELETE FROM long_term_memory WHERE conversation_id = ?",
                (conversation_id,),
            )
            self._conn.execute(
                "DELETE FROM short_term_image_refs WHERE conversation_id = ?",
                (conversation_id,),
            )
            self._conn.commit()

    def clear_short_term(self, conversation_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM short_term_memory WHERE conversation_id = ?",
                (conversation_id,),
            )
            self._conn.execute(
                "DELETE FROM short_term_image_refs WHERE conversation_id = ?",
                (conversation_id,),
            )
            self._conn.commit()

    def clear_long_term(self, conversation_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM long_term_memory WHERE conversation_id = ?",
                (conversation_id,),
            )
            self._conn.commit()

    def get_image_description(self, image_sha256: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT description FROM image_description_cache WHERE image_sha256 = ?",
                (image_sha256,),
            ).fetchone()
        if not row:
            return None
        return str(row["description"])

    def set_image_description(self, image_sha256: str, description: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO image_description_cache (image_sha256, description, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(image_sha256)
                DO UPDATE SET description = excluded.description, updated_at = excluded.updated_at
                """,
                (image_sha256, description, now),
            )
            self._conn.commit()

    def get_url_summary(self, url: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM url_summary_cache WHERE url = ?",
                (url,),
            ).fetchone()
        if not row:
            return None
        return str(row["summary"])

    def set_url_summary(self, url: str, summary: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO url_summary_cache (url, summary, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(url)
                DO UPDATE SET summary = excluded.summary, updated_at = excluded.updated_at
                """,
                (url, summary, now),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
