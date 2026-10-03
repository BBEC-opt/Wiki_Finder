"""Embedding 与对话模型提供方。"""

import hashlib
import json
import math
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from app.core.errors import AppError


class EmbeddingProvider(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class ChatProvider(ABC):
    @abstractmethod
    async def stream(self, messages: list[dict]) -> AsyncIterator[str]: ...

    async def complete(self, messages: list[dict]) -> str:
        parts = []
        async for delta in self.stream(messages):
            parts.append(delta)
        return "".join(parts)


class HashEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dimension: int):
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        values = [0.0] * self.dimension
        normalized = re.sub(r"\s+", "", text.lower())
        grams = [normalized[i:i + 2] for i in range(max(1, len(normalized) - 1))]
        for gram in grams:
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "big")
            values[raw % self.dimension] += 1.0 if raw & 1 else -1.0
        return normalize_vector(values)


class ExtractiveChatProvider(ChatProvider):
    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        prompt = messages[-1]["content"]
        refs = re.findall(r'<reference id="(\d+)"[^>]*>\s*([\s\S]*?)</reference>', prompt)
        if not refs:
            answer = "当前知识库中没有找到足以回答该问题的资料。"
        else:
            snippets = []
            for idx, text in refs[:3]:
                clean = re.sub(r"\s+", " ", text).strip()
                snippets.append(f"{clean[:260]} [{idx}]")
            answer = "根据知识库资料：\n\n" + "\n\n".join(snippets)
        for i in range(0, len(answer), 24):
            yield answer[i:i + 24]


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, base_url: str, api_key: str, model: str, dimension: int, timeout: int):
        self.url = base_url.rstrip("/") + "/embeddings"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.model, self.dimension, self.timeout = model, dimension, timeout
        # 百炼 text-embedding-v4 的同步接口单次最多接收 10 条文本。
        self.max_batch_size = 10 if model == "text-embedding-v4" else None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            payload = {"model": self.model, "input": texts}
            if self.model.startswith("text-embedding-3-") or self.model in {
                "text-embedding-v3", "text-embedding-v4", "qwen3.7-text-embedding",
                "qwen3.7-text-embedding-flash",
            }:
                payload["dimensions"] = self.dimension
            response = await client.post(self.url, headers=self.headers, json=payload)
            if response.is_error:
                detail = _provider_error_detail(response)
                error = AppError("embedding_provider_error", f"Embedding 服务返回 {response.status_code}：{detail}", 502)
                error.retryable = response.status_code == 429 or response.status_code >= 500
                raise error
            data = sorted(response.json()["data"], key=lambda x: x["index"])
        vectors = [normalize_vector(item["embedding"]) for item in data]
        if len(vectors) != len(texts):
            raise ValueError("Embedding response count mismatch")
        return vectors


class OpenAIChatProvider(ChatProvider):
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.model, self.timeout = model, timeout

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", self.url, headers=self.headers,
                                     json={"model": self.model, "messages": messages, "stream": True}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    payload = json.loads(line[6:])
                    delta = payload.get("choices", [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield delta


def normalize_vector(vector: list[float]) -> list[float]:
    if not vector or any(not math.isfinite(float(x)) for x in vector):
        raise ValueError("Embedding contains invalid values")
    norm = math.sqrt(sum(float(x) ** 2 for x in vector))
    if norm == 0:
        raise ValueError("Embedding is a zero vector")
    return [float(x) / norm for x in vector]


def _provider_error_detail(response: httpx.Response) -> str:
    """保留可诊断信息，但不回显请求头和 API Key。"""
    try:
        body = response.json()
        detail = body.get("error", body) if isinstance(body, dict) else body
        return json.dumps(detail, ensure_ascii=False)[:1000]
    except (ValueError, TypeError):
        return response.text[:1000] or response.reason_phrase


def create_providers(settings):
    if settings.model_api_key:
        return (
            OpenAIEmbeddingProvider(settings.model_base_url, settings.model_api_key,
                                    settings.embedding_model, settings.embedding_dimension,
                                    settings.model_timeout_seconds),
            OpenAIChatProvider(settings.model_base_url, settings.model_api_key,
                               settings.chat_model, settings.model_timeout_seconds),
        )
    return HashEmbeddingProvider(settings.embedding_dimension), ExtractiveChatProvider()
