"""可重建的 Dense 向量索引。"""

from __future__ import annotations

from typing import Any

import httpx


class QdrantVectorIndex:
    def __init__(self, base_url: str, api_key: str, collection_prefix: str, timeout: int = 10):
        headers = {"api-key": api_key} if api_key else {}
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)
        self.collection_prefix = collection_prefix

    def collection(self, dimension: int) -> str:
        return f"{self.collection_prefix}_chunks_v1_{dimension}"

    async def initialize(self) -> None:
        response = await self.client.get("/healthz")
        response.raise_for_status()

    async def close(self) -> None:
        await self.client.aclose()

    async def ensure_collection(self, dimension: int) -> str:
        name = self.collection(dimension)
        response = await self.client.get(f"/collections/{name}")
        if response.status_code == 404:
            created = await self.client.put(
                f"/collections/{name}", json={"vectors": {"size": dimension, "distance": "Cosine"}},
            )
            created.raise_for_status()
            for field in ("knowledge_base_id", "knowledge_id"):
                indexed = await self.client.put(
                    f"/collections/{name}/index", json={"field_name": field, "field_schema": "keyword"},
                )
                indexed.raise_for_status()
        else:
            response.raise_for_status()
        return name

    async def upsert(self, dimension: int, points: list[dict[str, Any]]) -> None:
        if not points:
            return
        name = await self.ensure_collection(dimension)
        response = await self.client.put(
            f"/collections/{name}/points", params={"wait": "true"}, json={"points": points},
        )
        response.raise_for_status()

    async def delete_knowledge(self, dimension: int, knowledge_id: str) -> None:
        name = await self.ensure_collection(dimension)
        response = await self.client.post(
            f"/collections/{name}/points/delete", params={"wait": "true"},
            json={"filter": {"must": [{"key": "knowledge_id", "match": {"value": knowledge_id}}]}},
        )
        response.raise_for_status()

    async def search(self, vector: list[float], kb_ids: list[str], knowledge_ids: list[str] | None,
                     limit: int, threshold: float) -> list[dict[str, Any]]:
        name = await self.ensure_collection(len(vector))
        must = [{"key": "knowledge_base_id", "match": {"any": kb_ids}}]
        if knowledge_ids:
            must.append({"key": "knowledge_id", "match": {"any": knowledge_ids}})
        response = await self.client.post(
            f"/collections/{name}/points/query",
            json={
                "query": vector, "filter": {"must": must}, "limit": limit,
                "score_threshold": threshold, "with_payload": True, "with_vector": False,
            },
        )
        response.raise_for_status()
        result = response.json().get("result", {})
        points = result.get("points", result if isinstance(result, list) else [])
        return [{"id": str(item["id"]), "score": float(item["score"])} for item in points]


def create_vector_index(settings):
    if settings.vector_index_driver == "qdrant":
        return QdrantVectorIndex(
            settings.qdrant_url, settings.qdrant_api_key,
            settings.qdrant_collection_prefix, settings.qdrant_timeout_seconds,
        )
    return None

