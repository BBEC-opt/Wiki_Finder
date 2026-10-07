import json
import math
import re
from collections import defaultdict

import jieba

from app.infrastructure.tracing import current_observation, traced
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


def tokenize_simple(text: str) -> set[str]:
    """简单分词用于 MMR 相似度计算。"""
    words = set()
    for token in jieba.cut(text.lower(), cut_all=False):
        token = token.strip()
        if len(token) > 1:
            words.add(token)
    return words


def jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """计算 Jaccard 相似度。"""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


class RetrievalService:
    def __init__(self, repository, embedding_provider, config, rerank_provider=None, vector_index=None):
        self.repo = repository
        self.embedding = embedding_provider
        self.config = config
        self.rerank_provider = rerank_provider
        self.vector_index = vector_index

    @traced("hybrid-search", "retriever")
    async def search(self, query: str, kb_ids: list[str], knowledge_ids: list[str] | None,
                     top_k: int, candidate_k: int = 20, threshold: float = 0.0) -> list[dict]:
        observation = current_observation()
        observation.update(input={"query_chars": len(query), "top_k": top_k, "candidate_k": candidate_k})
        observation.content(input=query)

        # 1. 向量检索
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

        # 2. 关键词检索
        sparse = []
        expression = fts_query(query)
        if expression:
            sparse = await self.repo.sparse_search(expression, kb_ids, knowledge_ids, candidate_k)

        # 3. RRF 融合（使用可配置参数）
        fused: dict[str, dict] = {}
        rrf_k = self.config.rrf_k
        vector_weight = self.config.vector_weight
        keyword_weight = self.config.keyword_weight

        for rank, (row, score) in enumerate(dense, 1):
            item = fused.setdefault(row["id"], self._result(row))
            item.update(dense_rank=rank, dense_score=score)
            item["fusion_score"] += vector_weight / (rrf_k + rank)

        for rank, row in enumerate(sparse, 1):
            item = fused.setdefault(row["id"], self._result(row))
            item.update(sparse_rank=rank, bm25_score=row["bm25_score"])
            item["fusion_score"] += keyword_weight / (rrf_k + rank)

        ordered = sorted(fused.values(), key=lambda x: x["fusion_score"], reverse=True)

        # 4. 去重
        seen, result = set(), []
        for item in ordered:
            signature = item["content_hash"]
            if signature in seen:
                continue
            seen.add(signature)
            result.append(item)

        # 5. Rerank（可选）
        rerank_rejected = 0
        if self.rerank_provider and result:
            result, rerank_rejected = await self._apply_rerank(query, result)

        # 6. MMR 多样性（可选）
        if self.config.mmr_enabled and len(result) > 1:
            result = self._apply_mmr(result, min(len(result), top_k * 2))

        # 7. 截断到 top_k
        result = result[:top_k]

        # 8. 重新编号
        for index, item in enumerate(result, 1):
            item["index"] = index

        output_meta = {
            "chunk_ids": [item["chunk_id"] for item in result],
            "count": len(result),
            "rrf_k": rrf_k,
            "vector_weight": vector_weight,
            "keyword_weight": keyword_weight,
        }
        if rerank_rejected > 0:
            output_meta["rerank_rejected"] = rerank_rejected
        if self.config.mmr_enabled:
            output_meta["mmr_applied"] = True

        observation.update(output=output_meta)
        observation.content(output=result)
        return result

    async def _apply_rerank(self, query: str, candidates: list[dict]) -> tuple[list[dict], int]:
        """应用 Reranker 模型重排序。"""
        if not candidates:
            return candidates, 0

        # 限制候选数量
        rerank_candidates = candidates[:self.config.rerank_top_n]
        passages = [item["context_content"] for item in rerank_candidates]

        try:
            # 调用 Reranker
            rerank_results = await self.rerank_provider.rerank(
                query, passages, top_n=None
            )

            # 应用阈值过滤
            threshold = self.config.rerank_threshold
            filtered = [r for r in rerank_results if r["score"] >= threshold]

            # 如果全部被过滤，保留最高分
            rejected_count = 0
            if not filtered and rerank_results:
                best = max(rerank_results, key=lambda x: x["score"])
                if best["score"] >= 0.15:  # 最低保底阈值
                    filtered = [best]
                else:
                    rejected_count = len(rerank_candidates)
            else:
                rejected_count = len(rerank_candidates) - len(filtered)

            # 重新排序并更新分数
            reranked = []
            for r in filtered:
                item = rerank_candidates[r["index"]].copy()
                base_score = item["fusion_score"]
                # 复合分数：60% rerank + 30% 原始融合分数 + 10% 位置先验
                position_prior = 1.0 - (item["chunk_index"] / 1000.0 * 0.05)
                item["rerank_score"] = r["score"]
                item["fusion_score"] = (
                    0.6 * r["score"] +
                    0.3 * min(base_score, 1.0) +
                    0.1 * max(0.95, min(1.0, position_prior))
                )
                reranked.append(item)

            reranked.sort(key=lambda x: x["fusion_score"], reverse=True)
            return reranked, rejected_count

        except Exception as e:
            import logging
            logging.warning(f"Rerank 失败，使用原始排序: {e}")
            return candidates, 0

    def _apply_mmr(self, candidates: list[dict], k: int) -> list[dict]:
        """应用 MMR 算法增加结果多样性。"""
        if k <= 0 or len(candidates) <= 1:
            return candidates

        k = min(k, len(candidates))
        lambda_param = self.config.mmr_lambda

        # 预分词
        token_sets = [tokenize_simple(item["context_content"]) for item in candidates]

        selected = []
        remaining = list(range(len(candidates)))
        max_redundancy = [0.0] * len(candidates)

        while len(selected) < k and remaining:
            best_idx = -1
            best_score = -1.0

            for i in remaining:
                item = candidates[i]
                relevance = item["fusion_score"]
                diversity = 1.0 - max_redundancy[i]
                mmr_score = lambda_param * relevance + (1.0 - lambda_param) * diversity

                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = i

            if best_idx == -1:
                break

            selected.append(best_idx)
            remaining.remove(best_idx)

            # 更新剩余候选的最大冗余度
            selected_tokens = token_sets[best_idx]
            for i in remaining:
                similarity = jaccard_similarity(token_sets[i], selected_tokens)
                max_redundancy[i] = max(max_redundancy[i], similarity)

        return [candidates[i] for i in selected]

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
