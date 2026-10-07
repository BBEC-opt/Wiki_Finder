"""PostgreSQL 连接适配器。

Repository 仍使用轻量 DB-API 风格接口；本模块只负责连接池、占位符和行对象适配，
避免 PostgreSQL 客户端类型泄漏到业务层。
"""

from __future__ import annotations

import re
from typing import Any

from app.infrastructure.database import SCHEMA


class PostgresCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None, rowcount: int = 0):
        self._rows = rows or []
        self.rowcount = rowcount
        self.lastrowid = self._rows[0].get("id") if self._rows else None

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


def _bind(sql: str) -> str:
    index = 0

    def replace(_: re.Match[str]) -> str:
        nonlocal index
        index += 1
        return f"${index}"

    return re.sub(r"\?", replace, sql)


def _normalize(sql: str) -> str:
    sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
    if "INSERT OR REPLACE INTO wiki_page_sources" in sql:
        sql = sql.replace("INSERT OR REPLACE INTO", "INSERT INTO")
        sql += " ON CONFLICT(page_id,knowledge_id,chunk_id) DO UPDATE SET page_number=excluded.page_number,excerpt=excluded.excerpt"
    return _bind(sql)


class PostgresConnection:
    def __init__(self, owner: "PostgreSQLDatabase", connection):
        self.owner = owner
        self.connection = connection
        self.in_transaction = False

    async def execute(self, sql: str, params: tuple | list = ()) -> PostgresCursor:
        normalized = _normalize(sql.strip())
        if normalized.upper() == "BEGIN":
            await self.connection.execute("BEGIN")
            self.in_transaction = True
            return PostgresCursor()
        returns_rows = normalized.lstrip().upper().startswith(("SELECT", "WITH")) or " RETURNING " in normalized.upper()
        if returns_rows:
            rows = await self.connection.fetch(normalized, *params)
            return PostgresCursor([dict(row) for row in rows], len(rows))
        status = await self.connection.execute(normalized, *params)
        match = re.search(r"(\d+)$", status)
        return PostgresCursor(rowcount=int(match.group(1)) if match else 0)

    async def executemany(self, sql: str, params: list[tuple]) -> None:
        if params:
            await self.connection.executemany(_normalize(sql), params)

    async def commit(self) -> None:
        if self.in_transaction:
            await self.connection.execute("COMMIT")
            self.in_transaction = False

    async def rollback(self) -> None:
        if self.in_transaction:
            await self.connection.execute("ROLLBACK")
            self.in_transaction = False

    async def close(self) -> None:
        if self.in_transaction:
            await self.rollback()
        await self.owner.pool.release(self.connection)


def _postgres_schema() -> list[str]:
    statements: list[str] = []
    for raw in SCHEMA.split(";"):
        statement = raw.strip()
        if not statement or statement.startswith("PRAGMA") or "VIRTUAL TABLE" in statement:
            continue
        statement = statement.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        statements.append(statement)
    statements.extend([
        "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS turn_id TEXT",
        "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'",
        """INSERT INTO chat_sessions(id,title,created_at,updated_at)
            SELECT session_id,'旧会话',MIN(created_at),MAX(created_at) FROM chat_messages GROUP BY session_id
            ON CONFLICT(id) DO NOTHING""",
        """CREATE TABLE IF NOT EXISTS chunks_fts (
          chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
          title_tokens TEXT NOT NULL, heading_tokens TEXT NOT NULL, content_tokens TEXT NOT NULL,
          search_vector tsvector GENERATED ALWAYS AS
            (to_tsvector('simple', title_tokens || ' ' || heading_tokens || ' ' || content_tokens)) STORED
        )""",
        "CREATE INDEX IF NOT EXISTS idx_chunks_fts_vector ON chunks_fts USING GIN(search_vector)",
        """CREATE TABLE IF NOT EXISTS outbox_events (
          id TEXT PRIMARY KEY, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
          event_type TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, locked_until TEXT,
          created_at TEXT NOT NULL, processed_at TEXT, last_error TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox_events(status, available_at)",
    ])
    return statements


class PostgreSQLDatabase:
    dialect = "postgresql"

    def __init__(self, url: str, min_size: int = 1, max_size: int = 10):
        self.url = url
        self.min_size = min_size
        self.max_size = max_size
        self.pool = None

    async def initialize(self) -> None:
        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError('PostgreSQL 模式需要安装项目的 "production" 可选依赖') from exc
        self.pool = await asyncpg.create_pool(
            self.url, min_size=self.min_size, max_size=self.max_size,
            command_timeout=30, server_settings={"timezone": "UTC"},
        )
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                for statement in _postgres_schema():
                    await connection.execute(statement)

    async def connect(self) -> PostgresConnection:
        if self.pool is None:
            raise RuntimeError("PostgreSQL connection pool is not initialized")
        return PostgresConnection(self, await self.pool.acquire())

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

