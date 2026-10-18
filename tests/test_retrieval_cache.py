"""查询向量缓存不改变实时检索和资料范围。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.retrieval import RetrievalService


@pytest.fixture
def service():
    config = Settings(_env_file=None, query_embedding_cache_size=2,
                      query_embedding_cache_ttl_seconds=10, mmr_enabled=False)
    repo = AsyncMock()
    repo.candidate_chunks.return_value = []
    repo.sparse_search.return_value = []
    embedding = AsyncMock()
    embedding.dimension = 8
    embedding.embed.return_value = [[1.0] + [0.0] * 7]
    return RetrievalService(repo, embedding, config)


async def test_hit_keeps_live_retrieval_and_scope(service):
    await service.search("查询", ["kb1"], ["doc1"], 3)
    row = dict(id="new", knowledge_id="doc2", knowledge_base_id="kb2",
               knowledge_title="新资料", chunk_index=0, heading_path="", page_number=1,
               content="最新内容", content_hash="new", embedding="[1,0,0,0,0,0,0,0]")
    service.repo.candidate_chunks.return_value = [row]
    results = await service.search("查询", ["kb2"], ["doc2"], 1)
    assert service.embedding.embed.await_count == 1
    assert service.repo.candidate_chunks.await_count == 2
    service.repo.candidate_chunks.assert_awaited_with(["kb2"], ["doc2"])
    assert service.repo.sparse_search.await_count == 2
    assert results[0]["chunk_id"] == "new"
    assert results[0]["knowledge_base_id"] == "kb2"


async def test_ttl_starts_at_write_and_hits_do_not_extend_it(service, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("app.services.retrieval.monotonic", lambda: now[0])

    async def embed(texts):
        now[0] += 5
        return [[1.0] + [0.0] * 7]

    service.embedding.embed.side_effect = embed
    await service._query_vector("a")  # 写入时为 5，过期时间为 15。
    now[0] = 12
    await service._query_vector("a")
    assert service.embedding.embed.await_count == 1
    now[0] = 15
    await service._query_vector("a")
    assert service.embedding.embed.await_count == 2


async def test_lru_hit_refreshes_recency(service):
    for query in ["a", "b", "a", "c", "a", "b"]:
        await service._query_vector(query)
    assert [call.args[0] for call in service.embedding.embed.await_args_list] == [
        ["a"], ["b"], ["c"], ["b"],
    ]
    assert len(service._query_vectors) == 2


async def test_disabled_cache_calls_provider_each_time(service):
    service.config.query_embedding_cache_size = 0
    await service._query_vector("a")
    await service._query_vector("a")
    assert service.embedding.embed.await_count == 2
    assert not service._query_vectors


@pytest.mark.parametrize("error", [RuntimeError("失败"), asyncio.CancelledError()])
async def test_failure_and_cancellation_are_not_cached(service, error):
    service.embedding.embed.side_effect = [error, [[1.0] + [0.0] * 7]]
    with pytest.raises(type(error)):
        await service._query_vector("a")
    await service._query_vector("a")
    await service._query_vector("a")
    assert service.embedding.embed.await_count == 2


@pytest.mark.parametrize("vector", [[], [0.0] * 8, [float("nan")] * 8,
                                    [float("inf")] * 8, [1.0]])
async def test_invalid_vectors_are_not_cached(service, vector):
    service.embedding.embed.return_value = [vector]
    await service._query_vector("a")
    await service._query_vector("a")
    assert service.embedding.embed.await_count == 2
    assert not service._query_vectors


async def test_exact_text_and_vector_copy(service):
    first = await service._query_vector("A")
    first[0] = 99
    hit = await service._query_vector("A")
    assert hit[0] == 1
    hit[0] = 88
    assert (await service._query_vector("A"))[0] == 1
    await service._query_vector("a")
    await service._query_vector(" A")
    assert service.embedding.embed.await_count == 3


@pytest.mark.parametrize("overrides", [dict(query_embedding_cache_size=-1),
                                       dict(query_embedding_cache_ttl_seconds=0)])
def test_cache_settings_reject_invalid_values(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


async def test_service_instances_do_not_share_vectors(service):
    other = RetrievalService(service.repo, service.embedding, service.config)
    await service._query_vector("a")
    await other._query_vector("a")
    assert service.embedding.embed.await_count == 2


async def test_expired_recent_entry_does_not_evict_live_entry(service, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("app.services.retrieval.monotonic", lambda: now[0])
    await service._query_vector("a")
    now[0] = 5
    await service._query_vector("b")
    await service._query_vector("a")
    now[0] = 10
    await service._query_vector("c")
    await service._query_vector("b")
    assert service.embedding.embed.await_count == 3
