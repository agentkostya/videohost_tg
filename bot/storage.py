"""Хранилище состояния: доступ пользователей + активные задания (SQLite)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class Job:
    id: str
    user_id: int
    chat_id: int
    url: str
    title: str = ""
    duration: int = 0
    fmt_id: str = ""            # выбранный yt-dlp format id ("137+140", "best", ...)
    file_path: str = ""         # скачанный файл (или объединённый)
    total_size: int = 0         # размер файла в байтах
    status: str = "await_choice"  # await_choice|downloading|uploading|splitting|done|error|canceled
    progress: float = 0.0       # прогресс текущей фазы, 0..1
    phase_progress: float = 0.0  # прогресс загрузки/отправки (для чанков)
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> tuple:
        return (
            self.id,
            self.user_id,
            self.chat_id,
            self.url,
            self.title,
            self.duration,
            self.fmt_id,
            self.file_path,
            self.total_size,
            self.status,
            self.progress,
            self.phase_progress,
            self.error,
            json.dumps(self.extra, ensure_ascii=False),
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            url=row["url"],
            title=row["title"],
            duration=row["duration"],
            fmt_id=row["fmt_id"],
            file_path=row["file_path"],
            total_size=row["total_size"],
            status=row["status"],
            progress=row["progress"],
            phase_progress=row["phase_progress"],
            error=row["error"],
            extra=json.loads(row["extra"] or "{}"),
        )


class Storage:
    """Потокобезопасная обёртка над SQLite."""

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    granted_at INTEGER NOT NULL DEFAULT 0,
                    last_seen  INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    duration INTEGER NOT NULL DEFAULT 0,
                    fmt_id TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL DEFAULT '',
                    total_size INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'await_choice',
                    progress REAL NOT NULL DEFAULT 0,
                    phase_progress REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    extra TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
                """
            )

    # ---------------- users / access ----------------
    def touch_user(self, user_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO users(user_id, last_seen) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
                (user_id, int(time.time())),
            )

    def has_access(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT granted_at FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
        return bool(row and row["granted_at"])

    def grant_access(self, user_id: int) -> None:
        with self._lock, self._conn:
            self.touch_user(user_id)
            self._conn.execute(
                "UPDATE users SET granted_at=? WHERE user_id=?", (int(time.time()), user_id)
            )

    def revoke_access(self, user_id: int) -> None:
        with self._lock, self._conn:
            self.touch_user(user_id)
            self._conn.execute("UPDATE users SET granted_at=0 WHERE user_id=?", (user_id,))

    def all_granted(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id FROM users WHERE granted_at>0"
            ).fetchall()
        return [r["user_id"] for r in rows]

    # ---------------- jobs ----------------
    def create_job(self, user_id: int, chat_id: int, url: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], user_id=user_id, chat_id=chat_id, url=url)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs(id,user_id,chat_id,url,title,duration,fmt_id,file_path,"
                "total_size,status,progress,phase_progress,error,extra) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                job.to_row(),
            )
        return job

    def save_job(self, job: Job) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET title=?,duration=?,fmt_id=?,file_path=?,total_size=?,"
                "status=?,progress=?,phase_progress=?,error=?,extra=? WHERE id=?",
                (
                    job.title,
                    job.duration,
                    job.fmt_id,
                    job.file_path,
                    job.total_size,
                    job.status,
                    job.progress,
                    job.phase_progress,
                    job.error,
                    json.dumps(job.extra, ensure_ascii=False),
                    job.id,
                ),
            )

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def active_jobs_for_user(self, user_id: int) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE user_id=? AND status IN "
                "('await_choice','downloading','uploading','splitting') ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def unfinished_jobs(self) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status IN ('await_choice','downloading','uploading','splitting')"
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def cleanup_files(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jobs WHERE status IN ('done','error','canceled')")
