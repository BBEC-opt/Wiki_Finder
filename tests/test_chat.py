"""会话隔离、取消、恢复与多轮检索的离线回归。"""

import asyncio
import json
import sqlite3
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient

from app.api.schemas import ChatRequest
from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.database import Database
from app.infrastructure.providers import ChatProvider, ExtractiveChatProvider
from app.infrastructure.repository import Repository
from app.main import create_app
from app.services.rag import RagService


@pytest.fixture
def client(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "chat.db", upload_dir=tmp_path / "uploads",
                        artifact_dir=tmp_path / "artifacts", embedding_dimension=64)
    with TestClient(create_app(settings)) as client:
        yield client


def make_base(client, name="问答"):
    return client.post("/api/v1/knowledge-bases", json={"name": name}).json()["data"]["id"]


def test_sessions_crud_scope_pagination_and_retry_validation(client):
    kb, other = make_base(client), make_base(client, "另一知识库")
    session = client.post("/api/v1/sessions", json={"knowledge_base_id": kb}).json()["data"]
    sid = session["id"]
    assert client.get(f"/api/v1/sessions?knowledge_base_id={other}").json()["data"] == []
    body = {"session_id": sid, "query": "如何使用？", "knowledge_base_ids": [kb]}
    assert client.post("/api/v1/chat", json={**body, "query": "  "}).status_code == 422
    assert client.post("/api/v1/chat", json={**body, "knowledge_base_ids": [other]}).status_code == 409
    result = client.post("/api/v1/chat", json=body)
    assert "event: start" in result.text and "event: done" in result.text
    messages = client.get(f"/api/v1/sessions/{sid}/messages").json()["data"]
    assert [m["status"] for m in messages] == ["completed", "completed"]
    assert messages[0]["turn_id"] == messages[1]["turn_id"]
    assert client.post("/api/v1/chat", json={**body, "retry_message_id": messages[-1]["id"]}).status_code == 409
    page = client.get(f"/api/v1/sessions/{sid}/messages?limit=1").json()["data"]
    older = client.get(f"/api/v1/sessions/{sid}/messages", params={"limit": 1, "before": page[0]["id"]}).json()["data"]
    assert older + page == messages
    assert client.get(f"/api/v1/sessions/{sid}/messages?before=missing").status_code == 422
    assert client.patch(f"/api/v1/sessions/{sid}", json={"title": "使用指南"}).json()["data"]["title"] == "使用指南"
    assert client.patch(f"/api/v1/sessions/{sid}", json={"title": "  "}).status_code == 422
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 200
    assert client.get(f"/api/v1/sessions/{sid}/messages").json()["data"] == []
    assert client.post("/api/v1/sessions/missing/stop").status_code == 404


def test_document_scope_is_checked_before_stream(client):
    kb, other = make_base(client), make_base(client, "另一知识库")
    doc = client.post(f"/api/v1/knowledge-bases/{other}/knowledge/manual", json={"title": "资料", "content": "内容"}).json()["data"]
    assert client.post("/api/v1/sessions", json={"knowledge_base_id": kb, "knowledge_ids": [doc["id"]]}).status_code == 422
    session = client.post("/api/v1/sessions", json={"knowledge_base_id": other, "knowledge_ids": [doc["id"]]}).json()["data"]
    response = client.post("/api/v1/chat", json={"session_id": session["id"], "query": "问题", "knowledge_base_ids": [other]})
    assert response.status_code == 409


@pytest.fixture
async def service(tmp_path):
    database = Database(tmp_path / "rag.db")
    await database.initialize()
    repo = Repository(database)
    kb = await repo.create_kb({"name": "测试", "chunk_size": 300, "chunk_overlap": 30, "parent_chunk_size": 1000,
                               "embedding_provider": "offline", "embedding_model": "hash", "embedding_dimension": 64})
    session = await repo.create_session(kb["id"], [])

    class Retrieval:
        def __init__(self):
            self.queries = []

        async def search(self, query, *_args):
            self.queries.append(query)
            return [{"index": 9, "chunk_id": "chunk", "knowledge_id": "document", "knowledge_base_id": kb["id"],
                     "knowledge_title": "设备指南", "content": "设备支持离线使用，最长工作八小时。", "context_content": "设备支持离线使用，最长工作八小时。"}]

    retrieval = Retrieval()
    rag = RagService(repo, retrieval, ExtractiveChatProvider(), 12000)
    return rag, repo, ChatRequest(session_id=session["id"], query="设备有什么功能？", knowledge_base_ids=[kb["id"]])


async def collect(rag, request):
    return [event async for event in rag.stream(request)]


async def test_followup_uses_completed_history_and_citations_are_renumbered(service):
    rag, repo, request = service
    first = await collect(rag, request)
    assert any('"index":1' in event for event in first)
    await collect(rag, request.model_copy(update={"query": "它能工作多久？"}))
    assert "设备有什么功能" in rag.retrieval.queries[-1]
    assert "它能工作多久" in rag.retrieval.queries[-1]
    assert len(await repo.completed_history(request.session_id)) == 4


async def test_busy_gate_stop_partial_answer_and_retry(service):
    rag, repo, request = service
    closed = asyncio.Event()

    class Slow(ChatProvider):
        async def stream(self, messages):
            try:
                yield "已生成的部分 [1]"
                await asyncio.Event().wait()
            finally:
                closed.set()

    rag.chat = Slow()
    turn = await rag.prepare(request)
    with pytest.raises(AppError, match="正在生成"):
        await rag.prepare(request)
    with pytest.raises(AppError, match="先停止"):
        await repo.mutate_session(request.session_id, "delete")
    async with aclosing(rag.stream(request, turn)) as stream:
        async for event in stream:
            if "event: answer" in event:
                await repo.stop_chat(request.session_id)
    assert closed.is_set()
    messages = await repo.chat_history(request.session_id)
    assert messages[-1]["status"] == "stopped"
    assert messages[-1]["content"] == "已生成的部分 [1]"
    assert await repo.completed_history(request.session_id) == []
    rag.chat = ExtractiveChatProvider()
    await collect(rag, request.model_copy(update={"retry_message_id": messages[-1]["id"]}))
    retried = await repo.chat_history(request.session_id)
    assert len(retried) == 2 and retried[-1]["id"] == messages[-1]["id"]
    assert retried[-1]["status"] == "completed"


async def test_disconnect_closes_model_and_persists_partial(service):
    rag, repo, request = service
    closed = asyncio.Event()

    class Slow(ChatProvider):
        async def stream(self, messages):
            try:
                yield "部分回答"
                await asyncio.Event().wait()
            finally:
                closed.set()

    rag.chat = Slow()
    async with aclosing(rag.stream(request)) as stream:
        async for event in stream:
            if "event: answer" in event:
                break
    assert closed.is_set()
    message = (await repo.chat_history(request.session_id))[-1]
    assert message["content"] == "部分回答" and message["status"] == "stopped"
    assert (await repo.get_session(request.session_id))["active_turn"] is None


async def test_failure_is_sanitized_and_failed_turn_excluded(service):
    rag, repo, request = service

    class Broken(ChatProvider):
        async def stream(self, messages):
            yield "部分回答"
            raise RuntimeError("secret-provider-content")

    rag.chat = Broken()
    events = await collect(rag, request)
    assert "event: error" in events[-1]
    assert "secret-provider-content" not in "".join(events)
    assert (await repo.chat_history(request.session_id))[-1]["content"] == "部分回答"
    assert await repo.completed_history(request.session_id) == []


async def test_empty_evidence_skips_remote_model_and_rewrite_falls_back(service):
    rag, repo, request = service

    class Broken(ChatProvider):
        async def stream(self, messages):
            raise RuntimeError("unavailable")
            yield ""

    rag.chat = Broken()
    assert await rag.retrieval_query("追问", [{"role": "user", "content": "上文"}]) == "追问"

    async def empty(*_args):
        return []

    rag.retrieval.search = empty
    events = await collect(rag, request)
    assert '"status":"completed"' in events[-1]
    assert "没有找到" in "".join(events)


async def test_real_provider_rewrites_followup_and_bounds_evidence(service):
    rag, repo, request = service
    await collect(rag, request)
    seen = []

    class Model(ChatProvider):
        async def complete(self, messages):
            return "设备的工作时长是多少？"

        async def stream(self, messages):
            seen.extend(messages)
            yield "八小时 [1]"

    rag.chat = Model()
    rag.max_context_chars = 1000
    await collect(rag, request.model_copy(update={"query": "它呢？"}))
    assert rag.retrieval.queries[-1] == "设备的工作时长是多少？"
    assert 'id="1"' in seen[-1]["content"]
    assert 'id="9"' not in seen[-1]["content"]


async def test_stale_generation_recovery_preserves_text(service):
    rag, repo, request = service
    turn = await rag.prepare(request)
    await repo.checkpoint_chat(request.session_id, turn["turn_id"], "已保存的内容", [])
    await repo.execute("UPDATE chat_sessions SET updated_at='2000-01-01' WHERE id=?", (request.session_id,))
    await repo.recover_chat(request.session_id)
    message = (await repo.chat_history(request.session_id))[-1]
    assert message["status"] == "stopped" and message["content"] == "已保存的内容"
    assert (await repo.get_session(request.session_id))["active_turn"] is None


async def test_legacy_migration_is_idempotent_and_keeps_unknown_scope(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE chat_messages (id TEXT PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,references_json TEXT,created_at TEXT)")
        db.execute("INSERT INTO chat_messages VALUES ('m','old','user','历史问题',NULL,'2026-01-01')")
    database = Database(path)
    await database.initialize()
    await database.initialize()
    repo = Repository(database)
    sessions = await repo.list_sessions(None, legacy=True)
    assert len(sessions) == 1 and sessions[0]["knowledge_base_id"] is None
    assert (await repo.chat_history("old"))[0]["content"] == "历史问题"


async def test_concurrent_prepare_admits_one_turn(service):
    rag, repo, request = service
    results = await asyncio.gather(rag.prepare(request), rag.prepare(request), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, AppError) for result in results) == 1
    assert len(await repo.chat_history(request.session_id)) == 2


async def test_stop_interrupts_retrieval_before_first_answer(service):
    rag, repo, request = service
    started, closed = asyncio.Event(), asyncio.Event()

    async def slow_search(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    rag.retrieval.search = slow_search
    pending = asyncio.create_task(collect(rag, request))
    await asyncio.wait_for(started.wait(), 2)
    await repo.stop_chat(request.session_id)
    events = await asyncio.wait_for(pending, 2)
    assert closed.is_set() and '"status":"stopped"' in events[-1]
    assert (await repo.chat_history(request.session_id))[-1]["status"] == "stopped"


async def test_task_cancellation_releases_session(service):
    rag, repo, request = service
    started = asyncio.Event()

    async def slow_search(*_args):
        started.set()
        await asyncio.Event().wait()

    rag.retrieval.search = slow_search
    pending = asyncio.create_task(collect(rag, request))
    await asyncio.wait_for(started.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await repo.get_session(request.session_id))["active_turn"] is None
    assert (await repo.chat_history(request.session_id))[-1]["status"] == "stopped"


async def test_oversized_first_reference_is_bounded_and_escaped(service):
    rag, repo, request = service
    original = rag.retrieval.search
    captured = []

    async def long_search(*args):
        rows = await original(*args)
        rows[0]["context_content"] = "内容<&>" * 1000
        return rows

    class Model(ChatProvider):
        async def stream(self, messages):
            captured.extend(messages)
            yield "回答 [1]"

    rag.chat = Model()
    rag.retrieval.search = long_search
    rag.max_context_chars = 1000
    await collect(rag, request)
    prompt = captured[-1]["content"]
    assert len(prompt) < 1100
    assert "&lt;" in prompt and "内容<&>" not in prompt


async def test_deleted_source_keeps_saved_reference_snapshot(service):
    rag, repo, request = service
    await collect(rag, request)
    message = (await repo.chat_history(request.session_id))[-1]
    reference = json.loads(message["references_json"])[0]
    # 检索桩中的来源不存在，历史仍保留回答时的证据，不依赖原始表的外键。
    assert await repo.get_knowledge(reference["knowledge_id"]) is None
    assert reference["content"] == "设备支持离线使用，最长工作八小时。"
