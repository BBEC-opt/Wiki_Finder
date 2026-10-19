import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app.infrastructure.repository as repository_module
from app.core.config import Settings
from app.infrastructure.database import Database
from app.infrastructure.providers import HashEmbeddingProvider
from app.infrastructure.repository import Repository
from app.infrastructure.storage import LocalStorage
from app.infrastructure.task_queue import MemoryTaskQueue
from app.parsing import ParsingService
from app.services.ingestion import IngestionService, IngestionWorker


@pytest.fixture
async def ingestion(tmp_path: Path, monkeypatch):
    settings = Settings(
        _env_file=None, database_path=tmp_path / "test.db",
        upload_dir=tmp_path / "uploads", artifact_dir=tmp_path / "artifacts",
        temp_dir=tmp_path / "tmp", embedding_dimension=64,
    )
    database = Database(settings.database_path)
    await database.initialize()
    repo = Repository(database)
    storage = LocalStorage(settings.upload_dir, settings.artifact_dir)
    await storage.initialize()
    kb = await repo.create_kb({
        "name": "恢复测试", "chunk_size": 300, "chunk_overlap": 30,
        "parent_chunk_size": 600, "embedding_provider": "offline",
        "embedding_model": "hash", "embedding_dimension": 64,
    })
    service = IngestionService(repo, storage, MemoryTaskQueue(), settings)
    knowledge = await service.create_manual(kb["id"], "恢复指南", "# 恢复指南\n\n长按按钮十秒可恢复设置。")
    task = await repo.query_one("SELECT * FROM ingestion_tasks WHERE knowledge_id=?", (knowledge["id"],))

    class Clock(datetime):
        current = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(repository_module, "datetime", Clock)
    # 数据库时钟可控，仅缩短队列等待，避免测试实际等待五分钟租约。
    monkeypatch.setattr(IngestionWorker, "_lease_interval", property(lambda self: 0.01))
    worker = IngestionWorker(
        repo, ParsingService(settings.artifact_dir), HashEmbeddingProvider(64),
        MemoryTaskQueue(), settings, storage=storage,
    )
    completed = asyncio.Event()
    succeed = repo.task_succeeded

    async def record_completion(task_id):
        await succeed(task_id)
        completed.set()

    monkeypatch.setattr(repo, "task_succeeded", record_completion)
    yield repo, worker, task, Clock, completed
    await worker.stop()
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "retry"])
async def test_restart_recovers_tasks_that_become_due_after_start(ingestion, state):
    repo, worker, task, clock, completed = ingestion
    claimed = await repo.claim_task(task["id"])
    await repo.mark_processing(task["knowledge_id"])
    if state == "retry":
        assert await repo.task_failed(claimed, "临时故障", retryable=True)
        assert await repo.claim_task(task["id"]) is None
    # 新队列模拟重启；启动时租约/退避尚未到期，不应抢占或提前重试。
    await worker.start()
    assert not completed.is_set()
    row = await repo.query_one("SELECT * FROM ingestion_tasks WHERE id=?", (task["id"],))
    assert row["attempts"] == 1
    assert row["status"] == ("running" if state == "running" else "queued")
    clock.current += timedelta(seconds=301)
    await asyncio.wait_for(completed.wait(), timeout=5)
    row = await repo.query_one("SELECT * FROM ingestion_tasks WHERE id=?", (task["id"],))
    assert row["status"] == "succeeded"
    assert row["attempts"] == 2
    knowledge = await repo.get_knowledge(task["knowledge_id"])
    assert knowledge["parse_status"] == "completed"
    assert await repo.list_chunks(knowledge["id"])
    assert await repo.list_wiki_pages(knowledge["knowledge_base_id"])


@pytest.mark.asyncio
async def test_long_task_renews_lease_and_stop_ends_heartbeat(ingestion, monkeypatch):
    repo, worker, task, clock, _ = ingestion
    entered, renewed = asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()
    renew = repo.renew_task_lease
    renewals = []
    allow_renewal = asyncio.Event()

    async def blocked_parse(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def record_renewal(task_id, attempt):
        await allow_renewal.wait()
        result = await renew(task_id, attempt)
        renewals.append(result)
        renewed.set()
        return result

    monkeypatch.setattr(worker.parser, "parse", blocked_parse)
    monkeypatch.setattr(repo, "renew_task_lease", record_renewal)
    await worker.start()
    await asyncio.wait_for(entered.wait(), timeout=5)
    clock.current += timedelta(seconds=301)
    renewed.clear()
    allow_renewal.set()
    await asyncio.wait_for(renewed.wait(), timeout=5)
    assert renewals[-1] is True
    # 开始时间已过期，但续租有效，另一个 Worker 的恢复扫描不得抢走任务。
    assert await repo.recover_tasks(300) == []
    await worker.stop()
    assert cancelled.is_set()
    count = len(renewals)
    await asyncio.sleep(0.04)
    assert len(renewals) == count
    clock.current += timedelta(seconds=301)
    assert await repo.recover_tasks(300) == [task["id"]]


@pytest.mark.asyncio
async def test_old_attempt_cannot_renew_reclaimed_task(ingestion):
    repo, _, task, clock, _ = ingestion
    old = await repo.claim_task(task["id"])
    clock.current += timedelta(seconds=301)
    assert await repo.recover_tasks(300) == [task["id"]]
    assert not await repo.renew_task_lease(task["id"], old["attempts"])
    current = await repo.claim_task(task["id"])
    assert current["attempts"] == old["attempts"] + 1
    assert not await repo.renew_task_lease(task["id"], old["attempts"])
    assert await repo.renew_task_lease(task["id"], current["attempts"])


@pytest.mark.asyncio
async def test_lost_lease_cancels_processing_without_stopping_worker(ingestion, monkeypatch):
    repo, worker, task, _, _ = ingestion
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_parse(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(worker.parser, "parse", blocked_parse)
    await worker.start()
    await asyncio.wait_for(entered.wait(), timeout=5)
    await repo.execute("UPDATE ingestion_tasks SET attempts=attempts+1 WHERE id=?", (task["id"],))
    await asyncio.wait_for(cancelled.wait(), timeout=5)
    await asyncio.wait_for(worker.queue.queue.join(), timeout=5)
    assert not worker.runner.done()
    row = await repo.query_one("SELECT * FROM ingestion_tasks WHERE id=?", (task["id"],))
    assert row["status"] == "running"
    assert row["attempts"] == 2
