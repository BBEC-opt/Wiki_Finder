import json
import math
import re
from collections import defaultdict

import jieba

from app.core.errors import AppError


def tokenize(text: str) -> list[str]:
    text = text.lower().strip()
    words = []
    for token in jieba.cut(text, cut_all=False):
        token = token.strip()
        if len(token) > 1 or re.fullmatch(r"[a-z0-9_]+", token):
            words.append(token)
    if not words:
        compact = re.sub(r"\s+", "", text)
        words = [compact[i:i + 2] for i in range(max(0, len(compact) - 1))]
    return list(dict.fromkeys(words))


def fts_query(text: str) -> str:
    tokens = [x.replace('"', '""') for x in tokenize(text)]
    return " OR ".join(f'"{x}"' for x in tokens)


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise AppError("embedding_dimension_mismatch", "查询向量与知识向量维度不一致", 409)
    return sum(a * b for a, b in zip(left, right))


class RetrievalService:
    def __init__(self, repository, embedding_provider, vector_index=None):
        self.repo = repository
        self.embedding = embedding_provider
        self.vector_index = vector_index

    async def search(self, query: str, kb_ids: list[str], knowledge_ids: list[str] | None,
                     top_k: int, candidate_k: int = 20, threshold: float = 0.0) -> list[dict]:
        query_vector = (await self.embedding.embed([query]))[0]
        dense = []
        if self.vector_index is None:
            candidates = await self.repo.candidate_chunks(kb_ids, knowledge_ids)
            for row in candidates:
                score = cosine(query_vector, json.loads(row["embedding"]))
                if score >= threshold:
                    dense.append((row, score))
            dense.sort(key=lambda x: x[1], reverse=True)
            dense = dense[:candidate_k]
        else:
            hits = await self.vector_index.search(query_vector, kb_ids, knowledge_ids, candidate_k, threshold)
            rows = {row["id"]: row for row in await self.repo.chunks_by_ids([hit["id"] for hit in hits])}
            dense = [(rows[hit["id"]], hit["score"]) for hit in hits if hit["id"] in rows]
        sparse = []
        expression = fts_query(query)
        if expression:
            sparse = await self.repo.sparse_search(expression, kb_ids, knowledge_ids, candidate_k)
        fused: dict[str, dict] = {}
        for rank, (row, score) in enumerate(dense, 1):
            item = fused.setdefault(row["id"], self._result(row))
            item.update(dense_rank=rank, dense_score=score)
            item["fusion_score"] += 1 / (60 + rank)
        for rank, row in enumerate(sparse, 1):
            item = fused.setdefault(row["id"], self._result(row))
            item.update(sparse_rank=rank, bm25_score=row["bm25_score"])
            item["fusion_score"] += 1 / (60 + rank)
        ordered = sorted(fused.values(), key=lambda x: x["fusion_score"], reverse=True)
        seen, result = set(), []
        for item in ordered:
            signature = item["content_hash"]
            if signature in seen:
                continue
            seen.add(signature)
            result.append(item)
            if len(result) >= top_k:
                break
        for index, item in enumerate(result, 1):
            item["index"] = index
        return result

    @staticmethod
    def _result(row: dict) -> dict:
        return {
            "index": 0, "chunk_id": row["id"], "knowledge_id": row["knowledge_id"],
            "knowledge_base_id": row["knowledge_base_id"], "knowledge_title": row["knowledge_title"],
            "chunk_index": row["chunk_index"], "heading_path": row["heading_path"],
            "page_number": row["page_number"], "content": row["content"], "content_hash": row["content_hash"],
            "context_content": row.get("parent_content") or row["content"],
            "dense_rank": None, "dense_score": None, "sparse_rank": None, "bm25_score": None,
            "fusion_score": 0.0,
        }
