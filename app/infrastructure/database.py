"""SQLite 连接与 Schema 初始化。"""

import aiosqlite
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS knowledge_bases (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  chunk_size INTEGER NOT NULL, chunk_overlap INTEGER NOT NULL, parent_chunk_size INTEGER NOT NULL DEFAULT 3200,
  embedding_provider TEXT NOT NULL, embedding_model TEXT NOT NULL,
  embedding_dimension INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledges (
  id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  title TEXT NOT NULL, source_type TEXT NOT NULL, file_name TEXT, file_path TEXT, file_hash TEXT,
  parse_status TEXT NOT NULL, current_stage TEXT NOT NULL DEFAULT 'queued', parser_engine TEXT,
  chunk_count INTEGER NOT NULL DEFAULT 0, error_message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, processed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_hash ON knowledges(knowledge_base_id, file_hash) WHERE file_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS parent_chunks (
  id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL REFERENCES knowledges(id) ON DELETE CASCADE,
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  parent_index INTEGER NOT NULL, heading_path TEXT NOT NULL DEFAULT '', page_number INTEGER,
  content TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(knowledge_id, parent_index)
);
CREATE INDEX IF NOT EXISTS idx_parent_chunks_knowledge ON parent_chunks(knowledge_id);
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL REFERENCES knowledges(id) ON DELETE CASCADE,
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL, heading_path TEXT NOT NULL DEFAULT '', page_number INTEGER,
  content TEXT NOT NULL, parent_content TEXT, parent_id TEXT REFERENCES parent_chunks(id) ON DELETE SET NULL,
  embedding TEXT NOT NULL,
  embedding_dimension INTEGER NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(knowledge_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_kb ON chunks(knowledge_base_id);
CREATE INDEX IF NOT EXISTS idx_chunks_knowledge ON chunks(knowledge_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, title_tokens, heading_tokens, content_tokens);
CREATE TABLE IF NOT EXISTS ingestion_tasks (
  id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL REFERENCES knowledges(id) ON DELETE CASCADE,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
  available_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON ingestion_tasks(status, available_at);
CREATE TABLE IF NOT EXISTS processing_stages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, knowledge_id TEXT NOT NULL REFERENCES knowledges(id) ON DELETE CASCADE,
  stage TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  input_summary TEXT, output_summary TEXT, error_code TEXT, error_message TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  references_json TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, created_at);
CREATE TABLE IF NOT EXISTS wiki_pages (
  id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  knowledge_id TEXT REFERENCES knowledges(id) ON DELETE SET NULL,
  slug TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
  page_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'published', folder_id TEXT,
  edit_source TEXT NOT NULL DEFAULT 'pipeline', version INTEGER NOT NULL DEFAULT 1,
  position INTEGER NOT NULL DEFAULT 0,
  source_refs TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(knowledge_base_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_kb ON wiki_pages(knowledge_base_id, position, created_at);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_knowledge ON wiki_pages(knowledge_id, position);
CREATE TABLE IF NOT EXISTS wiki_folders (
  id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  parent_id TEXT REFERENCES wiki_folders(id) ON DELETE CASCADE, name TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(knowledge_base_id, parent_id, name)
);
CREATE TABLE IF NOT EXISTS wiki_page_sources (
  page_id TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  knowledge_id TEXT NOT NULL REFERENCES knowledges(id) ON DELETE CASCADE,
  chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL, page_number INTEGER, excerpt TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(page_id, knowledge_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_wiki_sources_knowledge ON wiki_page_sources(knowledge_id);
CREATE TABLE IF NOT EXISTS wiki_page_links (
  source_page_id TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  target_page_id TEXT REFERENCES wiki_pages(id) ON DELETE SET NULL, target_slug TEXT NOT NULL,
  PRIMARY KEY(source_page_id, target_slug)
);
CREATE TABLE IF NOT EXISTS wiki_builds (
  id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  knowledge_id TEXT NOT NULL REFERENCES knowledges(id) ON DELETE CASCADE,
  status TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'offline',
  started_at TEXT NOT NULL, finished_at TEXT, error_message TEXT NOT NULL DEFAULT '', stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_wiki_builds_knowledge ON wiki_builds(knowledge_id, started_at);
CREATE TABLE IF NOT EXISTS wiki_build_stages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, build_id TEXT NOT NULL REFERENCES wiki_builds(id) ON DELETE CASCADE,
  stage TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  input_json TEXT NOT NULL DEFAULT '{}', output_json TEXT NOT NULL DEFAULT '{}', error_message TEXT NOT NULL DEFAULT '',
  UNIQUE(build_id, stage)
);
CREATE TABLE IF NOT EXISTS wiki_page_contributions (
  page_id TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  knowledge_id TEXT NOT NULL REFERENCES knowledges(id) ON DELETE CASCADE,
  summary TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', chunk_ids_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL, PRIMARY KEY(page_id, knowledge_id)
);
CREATE TABLE IF NOT EXISTS wiki_page_issues (
  id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  page_id TEXT REFERENCES wiki_pages(id) ON DELETE CASCADE, issue_type TEXT NOT NULL,
  detail TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', build_id TEXT REFERENCES wiki_builds(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(knowledge_base_id, page_id, issue_type, detail)
);
CREATE TABLE IF NOT EXISTS outbox_events (
  id TEXT PRIMARY KEY, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
  event_type TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, locked_until TEXT,
  created_at TEXT NOT NULL, processed_at TEXT, last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox_events(status, available_at);
"""


class Database:
    dialect = "sqlite"
    def __init__(self, path: Path):
        self.path = path

    async def connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        return db

    async def initialize(self) -> None:
        db = await self.connect()
        try:
            await self._migrate_legacy_wiki(db)
            await db.executescript(SCHEMA)
            await self._migrate_parent_chunks(db)
            await db.commit()
        finally:
            await db.close()

    async def close(self) -> None:
        """SQLite 每次操作使用短连接，无需关闭共享资源。"""

    @staticmethod
    async def _migrate_legacy_wiki(db: aiosqlite.Connection) -> None:
        """将早期“每文档一组页面”的表升级为知识库级 Wiki。"""
        table = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_pages'")
        if not await table.fetchone():
            return
        columns = {row[1]: row for row in await (await db.execute("PRAGMA table_info(wiki_pages)")).fetchall()}
        if "status" in columns and columns.get("knowledge_id", (None, None, None, 0))[3] == 0:
            return
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.executescript("""
        ALTER TABLE wiki_pages RENAME TO wiki_pages_legacy;
        CREATE TABLE wiki_pages (
          id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
          knowledge_id TEXT REFERENCES knowledges(id) ON DELETE SET NULL,
          slug TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
          page_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'published', folder_id TEXT,
          edit_source TEXT NOT NULL DEFAULT 'pipeline', version INTEGER NOT NULL DEFAULT 1,
          position INTEGER NOT NULL DEFAULT 0, source_refs TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(knowledge_base_id, slug)
        );
        INSERT INTO wiki_pages
        (id, knowledge_base_id, knowledge_id, slug, title, summary, content, page_type, position, source_refs, created_at, updated_at)
        SELECT id, knowledge_base_id, knowledge_id,
          CASE WHEN page_type='summary' THEN 'summary/' || knowledge_id
               WHEN page_type='source' THEN 'source/' || knowledge_id
               ELSE 'topic/' || knowledge_id || '/' || slug END,
          title, summary, content, page_type, position, source_refs, created_at, updated_at
        FROM wiki_pages_legacy;
        DROP TABLE wiki_pages_legacy;
        """)
        await db.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    async def _migrate_parent_chunks(db: aiosqlite.Connection) -> None:
        """为既有数据库补齐父子分块字段；旧块仍可回退到 parent_content。"""
        kb_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(knowledge_bases)")).fetchall()}
        if "parent_chunk_size" not in kb_columns:
            await db.execute("ALTER TABLE knowledge_bases ADD COLUMN parent_chunk_size INTEGER NOT NULL DEFAULT 3200")
        chunk_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(chunks)")).fetchall()}
        if "parent_id" not in chunk_columns:
            await db.execute("ALTER TABLE chunks ADD COLUMN parent_id TEXT REFERENCES parent_chunks(id) ON DELETE SET NULL")


def create_database(settings):
    if settings.database_url:
        from app.infrastructure.postgresql import PostgreSQLDatabase
        return PostgreSQLDatabase(
            settings.database_url,
            settings.database_pool_min_size,
            settings.database_pool_max_size,
        )
    return Database(settings.database_path)
