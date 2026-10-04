import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.infrastructure.providers import OpenAIChatProvider, OpenAIEmbeddingProvider
from app.infrastructure.tracing import Tracing, current_observation, traced
from app.main import create_app


@pytest.fixture
def recorder():
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from uuid import uuid4
    exporter = InMemorySpanExporter()
    client = langfuse.Langfuse(
        public_key="pk-lf-offline-tests-" + str(uuid4()), secret_key="test-placeholder",
        base_url="http://localhost:1", tracer_provider=TracerProvider(),
        span_exporter=exporter,
    )
    tracing = Tracing(client)
    yield tracing, exporter
    client.shutdown()


def spans(recorder):
    tracing, exporter = recorder
    tracing.client.flush()
    return exporter.get_finished_spans()


def test_config_requires_explicit_credentials_and_url():
    assert Tracing.from_settings(Settings(_env_file=None)).client is None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, langfuse_enabled=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, langfuse_enabled=True, langfuse_public_key="pk-test",
                 langfuse_secret_key="test-placeholder", langfuse_base_url="file:///tmp")


@pytest.mark.asyncio
async def test_stream_context_isolation_and_close(recorder):
    class Service:
        tracing = recorder[0]

        @traced("child", "generation")
        async def child(self):
            await asyncio.sleep(0)

        @traced("answer", session=True)
        async def stream(self, request):
            await self.child()
            yield request.session_id
            await self.child()
            yield "done"

    service = Service()
    first = service.stream(SimpleNamespace(session_id="session-a"))
    second = service.stream(SimpleNamespace(session_id="session-b"))
    assert await anext(first) == "session-a"
    assert current_observation().span is None
    assert await anext(second) == "session-b"
    await first.aclose()
    assert [item async for item in second] == ["done"]
    recorded = spans(recorder)
    roots = [s for s in recorded if s.name == "answer"]
    assert len(roots) == 2
    assert roots[0].context.trace_id != roots[1].context.trace_id
    for child in [s for s in recorded if s.name == "child"]:
        parent = next(s for s in roots if s.context.span_id == child.parent.span_id)
        assert child.context.trace_id == parent.context.trace_id
        assert child.attributes["session.id"] == parent.attributes["session.id"]


@pytest.mark.asyncio
async def test_provider_usage_and_content_policy(recorder, monkeypatch):
    captured = []
    async def respond(request):
        captured.append(request)
        if request.url.path == "/embeddings":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3, 4]}],
                                            "usage": {"prompt_tokens": 5, "total_tokens": 5}})
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"private-answer"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}\n\ndata: [DONE]\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    chat = OpenAIChatProvider("https://example.test", "test-placeholder", "test-chat", 5)
    embedding = OpenAIEmbeddingProvider("https://example.test", "test-placeholder", "test-embedding", 2, 5)
    chat.tracing = embedding.tracing = recorder[0]
    assert await chat.complete([{"role": "user", "content": "private-question"}]) == "private-answer"
    assert await embedding.embed(["private-document"]) == [[0.6, 0.8]]
    recorded = spans(recorder)
    import json
    generation = next(s for s in recorded if s.name == "model-chat")
    assert json.loads(generation.attributes["langfuse.observation.usage_details"])["output"] == 2
    assert "private-" not in str([dict(s.attributes) for s in recorded])
    assert b'include_usage' in captured[0].content
    recorder[0].capture_content = True
    await chat.complete([{"role": "user", "content": "private-question"}])
    assert "private-question" in str([dict(s.attributes) for s in spans(recorder)])


@pytest.mark.asyncio
async def test_tracing_failure_does_not_change_business_result(monkeypatch):
    pytest.importorskip("langfuse")
    class BrokenClient:
        def start_observation(self, **kwargs):
            raise RuntimeError("must-not-leak")

    class Service:
        tracing = Tracing(BrokenClient())

        @traced("operation")
        async def run(self):
            return 42

    assert await Service().run() == 42


def test_ingestion_search_and_sse_have_nested_traces(recorder, monkeypatch, tmp_path):
    import time
    monkeypatch.setattr(Tracing, "from_settings", lambda settings: recorder[0])
    settings = Settings(_env_file=None, database_path=tmp_path / "test.db",
                        upload_dir=tmp_path / "uploads", artifact_dir=tmp_path / "artifacts",
                        temp_dir=tmp_path / "tmp", model_api_key="", embedding_dimension=64)
    with TestClient(create_app(settings)) as client:
        kb = client.post("/api/v1/knowledge-bases", json={"name": "测试知识库"}).json()["data"]
        knowledge = client.post(f"/api/v1/knowledge-bases/{kb['id']}/knowledge/manual",
                                json={"title": "设备手册", "content": "# 重置\n长按按钮十秒即可重置设备。"}).json()["data"]
        for _ in range(100):
            state = client.get(f"/api/v1/knowledge/{knowledge['id']}").json()["data"]
            if state["parse_status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert state["parse_status"] == "completed"
        response = client.post("/api/v1/chat", json={"session_id": "session-e2e", "query": "如何重置设备",
                                                    "knowledge_base_ids": [kb["id"]]})
        assert "event: done" in response.text
        recorded = spans(recorder)
        by_name = {s.name: s for s in recorded}
        assert {"document-ingestion", "wiki-generation", "hybrid-search", "rag-answer", "offline-answer"} <= by_name.keys()
        assert by_name["hybrid-search"].parent.span_id == by_name["rag-answer"].context.span_id
        assert by_name["offline-answer"].parent.span_id == by_name["rag-answer"].context.span_id
        assert by_name["wiki-generation"].parent.span_id == by_name["document-ingestion"].context.span_id
        assert "长按按钮" not in str([dict(s.attributes) for s in recorded])


@pytest.mark.asyncio
async def test_cancellation_and_errors_end_spans_without_sensitive_details(recorder):
    started = asyncio.Event()

    class Service:
        tracing = recorder[0]

        @traced("cancelled-operation")
        async def wait(self):
            started.set()
            await asyncio.Event().wait()

        @traced("failed-operation")
        async def fail(self):
            raise ValueError("private-provider-response")

    task = asyncio.create_task(Service().wait())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ValueError):
        await Service().fail()
    recorded = spans(recorder)
    assert {s.name for s in recorded} == {"cancelled-operation", "failed-operation"}
    assert all(s.attributes["langfuse.observation.level"] == "ERROR" for s in recorded)
    assert "private-provider-response" not in str([dict(s.attributes) for s in recorded])
    assert current_observation().span is None


def test_disabled_tracing_does_not_import_sdk(monkeypatch):
    import builtins
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "langfuse":
            raise AssertionError("disabled tracing imported SDK")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    tracing = Tracing.from_settings(Settings(_env_file=None, langfuse_enabled=False))
    observation = tracing.start("offline", "span")
    observation.update(output={"ok": True})
    observation.end()


def test_sdk_initialization_failure_disables_tracing(monkeypatch, caplog):
    langfuse = pytest.importorskip("langfuse")
    def fail(**kwargs):
        raise RuntimeError("private-init-detail")
    monkeypatch.setattr(langfuse, "Langfuse", fail)
    settings = Settings(_env_file=None, langfuse_enabled=True, langfuse_public_key="pk-test",
                        langfuse_secret_key="test-placeholder", langfuse_base_url="https://example.test")
    assert Tracing.from_settings(settings).client is None
    assert "private-init-detail" not in caplog.text
