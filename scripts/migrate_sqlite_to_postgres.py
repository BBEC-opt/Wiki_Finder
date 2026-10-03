"""将 SQLite 数据可重入复制到已初始化的 PostgreSQL。

用法：python scripts/migrate_sqlite_to_postgres.py data/demo.db postgresql://...
迁移只插入缺失主键，不删除或覆盖目标数据；完成后逐表校验行数。
"""

import argparse
import asyncio
from pathlib import Path

import aiosqlite

from app.infrastructure.postgresql import PostgreSQLDatabase


TABLES = (
    "knowledge_bases", "knowledges", "parent_chunks", "chunks", "chunks_fts",
    "ingestion_tasks", "processing_stages", "chat_messages", "wiki_pages", "wiki_folders",
    "wiki_page_sources", "wiki_page_links", "wiki_builds", "wiki_build_stages",
    "wiki_page_contributions", "wiki_page_issues", "outbox_events",
)


async def migrate(source: Path, target_url: str) -> None:
    if not source.is_file():
        raise SystemExit(f"SQLite 文件不存在：{source}")
    target = PostgreSQLDatabase(target_url)
    await target.initialize()
    source_db = await aiosqlite.connect(source)
    source_db.row_factory = aiosqlite.Row
    try:
        connection = await target.pool.acquire()
        try:
            async with connection.transaction():
                for table in TABLES:
                    exists = await source_db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,))
                    if not await exists.fetchone():
                        continue
                    rows = await (await source_db.execute(f'SELECT * FROM "{table}"')).fetchall()
                    if not rows:
                        continue
                    columns = list(rows[0].keys())
                    names = ",".join(f'"{name}"' for name in columns)
                    binds = ",".join(f"${index}" for index in range(1, len(columns) + 1))
                    conflict = " ON CONFLICT DO NOTHING"
                    await connection.executemany(
                        f'INSERT INTO "{table}" ({names}) VALUES ({binds}){conflict}',
                        [tuple(row[name] for name in columns) for row in rows],
                    )
            for table in TABLES:
                exists = await source_db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,))
                if not await exists.fetchone():
                    continue
                source_count = (await (await source_db.execute(f'SELECT COUNT(*) FROM "{table}"')).fetchone())[0]
                target_count = await connection.fetchval(f'SELECT COUNT(*) FROM "{table}"')
                if target_count < source_count:
                    raise RuntimeError(f"{table} 校验失败：source={source_count}, target={target_count}")
                print(f"{table}: source={source_count}, target={target_count}")
        finally:
            await target.pool.release(connection)
    finally:
        await source_db.close()
        await target.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("database_url")
    args = parser.parse_args()
    asyncio.run(migrate(args.source, args.database_url))


if __name__ == "__main__":
    main()
