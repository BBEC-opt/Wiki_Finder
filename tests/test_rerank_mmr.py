"""测试 Reranker 和 MMR 功能。"""

import pytest

from app.infrastructure.rerank_provider import OfflineRerankProvider
from app.services.retrieval import tokenize_simple, jaccard_similarity


class TestOfflineReranker:
    """测试离线 Reranker。"""

    @pytest.fixture
    def reranker(self):
        return OfflineRerankProvider()

    async def test_rerank_basic(self, reranker):
        query = "如何恢复出厂设置"
        passages = [
            "产品支持多种颜色选择",
            "要恢复出厂设置，请按住电源键10秒",
            "出厂设置可以清除所有数据",
            "本产品重量为500克",
        ]

        results = await reranker.rerank(query, passages)

        assert len(results) == len(passages)
        assert all("index" in r and "score" in r for r in results)
        # 相关的内容（索引1和2）应该比不相关的（索引0和3）得分高
        relevant_indices = {results[0]["index"], results[1]["index"]}
        assert 1 in relevant_indices or 2 in relevant_indices
        assert results[0]["score"] > results[-1]["score"]

    async def test_rerank_empty_query(self, reranker):
        query = ""
        passages = ["内容1", "内容2", "内容3"]

        results = await reranker.rerank(query, passages)

        assert len(results) == len(passages)
        # 空查询时保持原序
        assert results[0]["index"] == 0
        assert results[1]["index"] == 1

    async def test_rerank_top_n(self, reranker):
        query = "测试"
        passages = ["测试内容"] * 10

        results = await reranker.rerank(query, passages, top_n=3)

        assert len(results) == 3


class TestMMRHelpers:
    """测试 MMR 辅助函数。"""

    def test_tokenize_simple(self):
        text = "这是一个测试文本，包含中文和English"
        tokens = tokenize_simple(text)

        assert isinstance(tokens, set)
        assert "测试" in tokens
        assert "文本" in tokens
        assert "english" in tokens  # 应该转小写

    def test_jaccard_similarity(self):
        set_a = {"测试", "文本", "内容"}
        set_b = {"测试", "文本", "数据"}

        similarity = jaccard_similarity(set_a, set_b)

        # 交集2个，并集4个，相似度=0.5
        assert 0.4 < similarity < 0.6

    def test_jaccard_similarity_identical(self):
        set_a = {"a", "b", "c"}
        set_b = {"a", "b", "c"}

        similarity = jaccard_similarity(set_a, set_b)
        assert similarity == 1.0

    def test_jaccard_similarity_disjoint(self):
        set_a = {"a", "b"}
        set_b = {"c", "d"}

        similarity = jaccard_similarity(set_a, set_b)
        assert similarity == 0.0

    def test_jaccard_similarity_empty(self):
        set_a = set()
        set_b = {"a", "b"}

        similarity = jaccard_similarity(set_a, set_b)
        assert similarity == 0.0


class TestMMRIntegration:
    """测试 MMR 算法集成。"""

    def test_mmr_diversity(self):
        """测试 MMR 是否能增加结果多样性。"""
        from app.services.retrieval import RetrievalService

        # 构造模拟候选
        candidates = [
            {
                "chunk_id": "1", "content": "恢复出厂设置的方法",
                "context_content": "恢复出厂设置的方法是按住电源键",
                "fusion_score": 0.9
            },
            {
                "chunk_id": "2", "content": "如何恢复出厂设置",
                "context_content": "如何恢复出厂设置请参考说明书",
                "fusion_score": 0.85
            },
            {
                "chunk_id": "3", "content": "产品保修政策",
                "context_content": "产品保修政策为一年免费保修",
                "fusion_score": 0.7
            },
            {
                "chunk_id": "4", "content": "出厂设置恢复步骤",
                "context_content": "出厂设置恢复步骤第一步是关机",
                "fusion_score": 0.8
            },
        ]

        # 创建一个模拟 config
        class MockConfig:
            mmr_lambda = 0.7
            mmr_diversity_threshold = 0.85

        service = RetrievalService.__new__(RetrievalService)
        service.config = MockConfig()

        result = service._apply_mmr(candidates, k=3)

        assert len(result) == 3
        # 第一个应该是最高分
        assert result[0]["chunk_id"] == "1"
        # 应该包含多样性较高的结果（不全是关于出厂设置的）
        chunk_ids = {r["chunk_id"] for r in result}
        assert "3" in chunk_ids  # 保修政策应该被选中（多样性高）


class TestRerankIntegration:
    """测试 Rerank 集成。"""

    async def test_rerank_filtering(self):
        """测试 Rerank 阈值过滤。"""
        from app.services.retrieval import RetrievalService

        class MockRerankProvider:
            async def rerank(self, query, passages, top_n):
                return [
                    {"index": 0, "score": 0.8},
                    {"index": 1, "score": 0.2},  # 低于阈值
                    {"index": 2, "score": 0.5},
                ]

        class MockConfig:
            rerank_threshold = 0.3
            rerank_top_n = 10

        service = RetrievalService.__new__(RetrievalService)
        service.config = MockConfig()
        service.rerank_provider = MockRerankProvider()

        candidates = [
            {"chunk_id": f"c{i}", "context_content": f"内容{i}", "fusion_score": 0.5, "chunk_index": i}
            for i in range(3)
        ]

        result, rejected = await service._apply_rerank("测试", candidates)

        # 只有2个通过阈值
        assert len(result) == 2
        assert rejected == 1
        # 分数应该是复合分数
        assert all("rerank_score" in r for r in result)


@pytest.mark.asyncio
async def test_end_to_end_retrieval():
    """端到端测试完整检索流程（不依赖真实模型）。"""
    from app.infrastructure.rerank_provider import OfflineRerankProvider
    from app.services.retrieval import RetrievalService

    class MockConfig:
        rrf_k = 60
        vector_weight = 1.0
        keyword_weight = 1.0
        mmr_enabled = True
        mmr_lambda = 0.7
        mmr_diversity_threshold = 0.85
        rerank_threshold = 0.3
        rerank_top_n = 20

    class MockEmbeddingProvider:
        async def embed(self, texts):
            return [[0.1] * 10 for _ in texts]

    class MockRepo:
        async def candidate_chunks(self, kb_ids, knowledge_ids):
            return [
                {
                    "id": f"chunk_{i}",
                    "knowledge_id": "k1",
                    "knowledge_base_id": "kb1",
                    "knowledge_title": "测试文档",
                    "chunk_index": i,
                    "heading_path": "标题",
                    "page_number": 1,
                    "content": f"这是第{i}个测试内容",
                    "content_hash": f"hash_{i}",
                    "parent_content": f"这是第{i}个测试内容的上下文",
                    "embedding": "[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]",
                }
                for i in range(5)
            ]

        async def sparse_search(self, expression, kb_ids, knowledge_ids, top_k):
            return [
                {
                    "id": f"chunk_{i}",
                    "knowledge_id": "k1",
                    "knowledge_base_id": "kb1",
                    "knowledge_title": "测试文档",
                    "chunk_index": i,
                    "heading_path": "标题",
                    "page_number": 1,
                    "content": f"这是第{i}个测试内容",
                    "content_hash": f"hash_{i}",
                    "parent_content": f"这是第{i}个测试内容的上下文",
                    "bm25_score": 1.0 / (i + 1),
                }
                for i in range(3)
            ]

    config = MockConfig()
    embedding = MockEmbeddingProvider()
    rerank = OfflineRerankProvider()
    repo = MockRepo()

    service = RetrievalService(repo, embedding, config, rerank, vector_index=None)

    results = await service.search(
        query="测试查询",
        kb_ids=["kb1"],
        knowledge_ids=None,
        top_k=3,
        candidate_k=10,
    )

    assert len(results) <= 3
    assert all("chunk_id" in r for r in results)
    assert all("fusion_score" in r for r in results)
    # 验证编号
    for i, r in enumerate(results, 1):
        assert r["index"] == i
