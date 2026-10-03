import httpx
import pytest

from app.core.errors import AppError
from app.infrastructure.providers import OpenAIEmbeddingProvider


class FakeClient:
    def __init__(self, response, captured, **kwargs):
        self.response = response
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        self.captured.update(url=url, headers=headers, json=json)
        return self.response


@pytest.mark.asyncio
async def test_embedding_v4_sends_dimension_and_exposes_batch_limit(monkeypatch):
    captured = {}
    request = httpx.Request("POST", "https://example.test/embeddings")
    response = httpx.Response(200, request=request, json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient(response, captured, **kwargs))
    provider = OpenAIEmbeddingProvider("https://example.test", "secret", "text-embedding-v4", 2, 10)

    vectors = await provider.embed(["内容"])

    assert provider.max_batch_size == 10
    assert captured["json"] == {"model": "text-embedding-v4", "input": ["内容"], "dimensions": 2}
    assert vectors == [[0.6, 0.8]]


@pytest.mark.asyncio
async def test_embedding_error_keeps_safe_provider_detail(monkeypatch):
    captured = {}
    request = httpx.Request("POST", "https://example.test/embeddings")
    response = httpx.Response(400, request=request, json={"error": {"code": "BadRequest", "message": "batch too large"}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient(response, captured, **kwargs))
    provider = OpenAIEmbeddingProvider("https://example.test", "secret", "text-embedding-v4", 1024, 10)

    with pytest.raises(AppError) as caught:
        await provider.embed(["内容"])

    assert caught.value.code == "embedding_provider_error"
    assert caught.value.retryable is False
    assert "batch too large" in str(caught.value)
    assert "secret" not in str(caught.value)
