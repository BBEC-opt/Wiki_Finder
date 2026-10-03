from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.infrastructure.database import Database, create_database
from app.infrastructure.postgresql import PostgreSQLDatabase, _bind, _postgres_schema
from app.infrastructure.repository import Repository
from app.infrastructure.storage import LocalStorage, create_storage
from app.infrastructure.task_queue import MemoryTaskQueue
from app.infrastructure.vector_index import QdrantVectorIndex


def test_default_infrastructure_is_offline(tmp_path: Path):
    settings = Settings(
        _env_file=None, database_path=tmp_path / "demo.db",
        upload_dir=tmp_path / "uploads", artifact_dir=tmp_path / "artifacts",
        temp_dir=tmp_path / "tmp",
    )
    assert isinstance(create_database(settings), Database)
    assert isinstance(create_storage(settings), LocalStorage)
    assert settings.database_driver == "sqlite"


def test_external_worker_requires_redis():
    with pytest.raises(ValidationError, match="TASK_NOTIFIER_DRIVER=redis"):
        Settings(_env_file=None, run_embedded_worker=False)


def test_postgres_schema_and_bind_conversion():
    settings = Settings(_env_file=None, database_url="postgresql://user:pass@localhost/db")
    assert isinstance(create_database(settings), PostgreSQLDatabase)
    assert _bind("SELECT * FROM chunks WHERE id=? AND knowledge_id=?") == (
        "SELECT * FROM chunks WHERE id=$1 AND knowledge_id=$2"
    )
    schema = "\n".join(_postgres_schema())
    assert "CREATE TABLE IF NOT EXISTS knowledge_bases" in schema
    assert "CREATE TABLE IF NOT EXISTS chunks_fts" in schema
    assert "VIRTUAL TABLE" not in schema


@pytest.mark.asyncio
async def test_memory_queue_round_trip():
    queue = MemoryTaskQueue()
    await queue.initialize()
    await queue.put("task-1")
    assert await queue.get() == "task-1"
    await queue.task_done()


@pytest.mark.asyncio
async def test_replace_index_creates_idempotent_vector_outbox(tmp_path: Path):
    database = Database(tmp_path / "outbox.db")
    await database.initialize()
    repository = Repository(database, vector_outbox=True)
    kb = await repository.create_kb({
        "name": "kb", "chunk_size": 300, "chunk_overlap": 30, "parent_chunk_size": 600,
        "embedding_provider": "offline", "embedding_model": "hash", "embedding_dimension": 2,
    })
    knowledge, _ = await repository.create_knowledge_and_task({
        "id": "knowledge-1", "knowledge_base_id": kb["id"], "title": "doc",
        "source_type": "manual", "file_name": "doc.md", "file_path": "doc.md", "file_hash": None,
    }, 3)
    await repository.replace_index(knowledge, "markdown", [], [{
        "id": "chunk-1", "chunk_index": 0, "heading_path": "", "page_number": None,
        "content": "内容", "parent_id": None, "embedding": [1.0, 0.0], "content_hash": "hash",
        "title_tokens": "doc", "heading_tokens": "", "content_tokens": "内容",
    }])
    events = await repository.claim_outbox(10)
    assert [event["event_type"] for event in events] == ["vector.delete_knowledge", "vector.upsert"]


def test_qdrant_collection_is_dimension_isolated():
    index = QdrantVectorIndex("http://localhost:6333", "", "wiki")
    assert index.collection(256) == "wiki_chunks_v1_256"
    assert index.collection(1024) == "wiki_chunks_v1_1024"
