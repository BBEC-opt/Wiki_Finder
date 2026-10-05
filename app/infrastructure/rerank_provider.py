"""Rerank 模型提供方。"""

from abc import ABC, abstractmethod


class RerankProvider(ABC):
    @abstractmethod
    async def rerank(self, query: str, passages: list[str], top_n: int | None = None) -> list[dict]:
        """
        对 passages 进行重排序。

        返回格式: [{"index": int, "score": float}, ...]
        index 是原始 passages 列表中的索引，score 是相关性分数 [0, 1]。
        """
        pass


class OfflineRerankProvider(RerankProvider):
    """离线 Reranker：基于关键词匹配和 jieba 分词的启发式排序。"""

    async def rerank(self, query: str, passages: list[str], top_n: int | None = None) -> list[dict]:
        import jieba

        query_lower = query.lower()
        query_words = set(jieba.cut(query_lower, cut_all=False))
        query_words = {w.strip() for w in query_words if len(w.strip()) > 1}

        if not query_words:
            # 无查询词时保持原序
            results = [{"index": i, "score": 1.0 / (i + 1)} for i in range(len(passages))]
            return results[:top_n] if top_n else results

        scored = []
        for i, passage in enumerate(passages):
            passage_lower = passage.lower()
            passage_words = list(jieba.cut(passage_lower, cut_all=False))
            passage_words = [w.strip() for w in passage_words if len(w.strip()) > 1]

            if not passage_words:
                scored.append({"index": i, "score": 0.0})
                continue

            # 计算匹配分数
            match_count = sum(1 for w in passage_words if w in query_words)
            coverage = len([w for w in query_words if w in passage_words]) / len(query_words)

            # 启发式分数：覆盖率 70% + 匹配密度 30%
            match_density = min(match_count / len(passage_words), 1.0)
            score = coverage * 0.7 + match_density * 0.3
            scored.append({"index": i, "score": score})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_n] if top_n else scored


class LocalRerankProvider(RerankProvider):
    """本地 Reranker 模型（使用 FlagEmbedding）。"""

    def __init__(self, model_name: str, batch_size: int = 32):
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            try:
                from FlagEmbedding import FlagReranker
                self._model = FlagReranker(self.model_name, use_fp16=True)
            except ImportError:
                raise RuntimeError(
                    "本地 Reranker 需要安装依赖: pip install -e \".[rerank]\""
                )
            except Exception as e:
                raise RuntimeError(f"加载 Reranker 模型失败: {e}")

    async def rerank(self, query: str, passages: list[str], top_n: int | None = None) -> list[dict]:
        import asyncio

        self._ensure_model()

        if not passages:
            return []

        # FlagReranker 需要 [query, passage] 对
        pairs = [[query, passage] for passage in passages]

        # 在线程池中执行推理
        loop = asyncio.get_event_loop()
        scores = await loop.run_in_executor(
            None,
            lambda: self._model.compute_score(pairs, batch_size=self.batch_size, normalize=True)
        )

        # 转换为统一格式
        if isinstance(scores, (int, float)):
            scores = [scores]

        results = [{"index": i, "score": float(score)} for i, score in enumerate(scores)]
        results.sort(key=lambda x: x["score"], reverse=True)

        return results[:top_n] if top_n else results


def create_rerank_provider(config) -> RerankProvider | None:
    """根据配置创建 Reranker。"""
    if not config.rerank_enabled:
        return None

    if config.provider_mode == "offline":
        return OfflineRerankProvider()

    try:
        return LocalRerankProvider(
            model_name=config.rerank_model,
            batch_size=config.rerank_batch_size
        )
    except Exception as e:
        import logging
        logging.warning(f"无法加载本地 Reranker，回退到离线模式: {e}")
        return OfflineRerankProvider()
