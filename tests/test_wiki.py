import json

from app.domain import ChunkDraft, ParsedDocument
from app.services.wiki import WikiGenerationService


class FakeWikiChat:
    def __init__(self):
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        if "候选条目" in messages[0]["content"]:
            return json.dumps({"candidates": [{
                "slug": "concept/hybrid-search", "title": "混合检索", "page_type": "concept",
            }]}, ensure_ascii=False)
        if "逐 Chunk 标注证据" in messages[0]["content"]:
            return json.dumps({"citations": {"concept/hybrid-search": [0, 1]}}, ensure_ascii=False)
        if "规划简洁稳定的目录" in messages[0]["content"]:
            return json.dumps({"folders": {"concept/hybrid-search": "检索/基础"}}, ensure_ascii=False)
        return json.dumps({
            "summary": "融合稠密与稀疏检索的检索方式。",
            "content": "# 混合检索\n\n混合检索结合向量语义召回与关键词匹配。",
        }, ensure_ascii=False)


async def test_model_wiki_pipeline_extracts_candidates_and_grounded_pages():
    chat = FakeWikiChat()
    service = WikiGenerationService(chat, "openai")
    knowledge = {"id": "doc-1", "title": "检索设计.md"}
    parsed = ParsedDocument(markdown="# 检索设计\n\n混合检索说明。", parser_engine="builtin")
    chunks = [
        ChunkDraft(0, "向量检索处理语义相似内容。", "检索设计 / 向量召回", None),
        ChunkDraft(1, "关键词检索处理精确术语。", "检索设计 / 关键词召回", 2),
    ]

    pages, meta = await service.generate(knowledge, parsed, chunks, [])

    concept = next(page for page in pages if page["slug"] == "concept/hybrid-search")
    assert concept["page_type"] == "concept"
    assert concept["source_refs"][0]["chunk_indices"] == [0, 1]
    assert "向量语义召回" in concept["content"]
    assert concept["folder"] == "检索/基础"
    assert meta["mode"] == "model"
    assert meta["candidates"] == 1
    assert meta["cited_chunks"] == 2
    assert meta["phases"][-1] == "finalize"
    assert len(chat.calls) == 4


async def test_invalid_model_protocol_falls_back_to_offline_generation():
    class InvalidChat:
        async def complete(self, _messages):
            return "not-json"

    service = WikiGenerationService(InvalidChat(), "openai")
    knowledge = {"id": "doc-2", "title": "离线降级.md"}
    parsed = ParsedDocument(markdown="# 稳定主题\n\n" + "可验证内容。" * 12, parser_engine="builtin")
    chunks = [ChunkDraft(0, "可验证内容。" * 12, "稳定主题", 1)]

    pages, meta = await service.generate(knowledge, parsed, chunks, [])

    assert any(page["slug"] == "concept/稳定主题" for page in pages)
    assert meta["mode"] == "offline"


async def test_candidate_extraction_keeps_valid_batches_and_ranks_repeated_objects():
    class BatchedChat:
        def __init__(self):
            self.calls = 0

        async def complete(self, _messages):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"candidates": [
                    {"slug": "concept/early", "title": "前部次要项", "page_type": "concept"},
                    {"slug": "concept/core", "title": "核心对象", "page_type": "concept"},
                ]}, ensure_ascii=False)
            if self.calls == 2:
                return "invalid-json"
            return json.dumps({"candidates": [
                {"slug": "concept/core", "title": "核心对象", "page_type": "concept"},
                {"slug": "concept/late", "title": "后部对象", "page_type": "concept"},
            ]}, ensure_ascii=False)

    service = WikiGenerationService(BatchedChat(), "openai", max_candidates=2)
    chunks = [ChunkDraft(index, "有效内容。" * 10, f"章节 {index}", index + 1) for index in range(17)]

    candidates, used_model = await service._candidates({"id": "doc", "title": "文档"}, chunks, [])

    assert used_model is True
    assert [item["title"] for item in candidates] == ["核心对象", "前部次要项"]


def test_focused_granularity_uses_smaller_candidate_budget():
    focused = WikiGenerationService(extraction_granularity="focused", max_candidates=12)
    standard = WikiGenerationService(extraction_granularity="standard", max_candidates=12)

    assert focused._candidate_limit() == 6
    assert standard._candidate_limit() == 12


async def test_deduplication_merges_normalized_titles_and_keeps_canonical_metadata():
    service = WikiGenerationService(provider_mode="offline")
    candidates = [
        {"slug": "concept/rag-a", "title": "RAG（检索增强生成）", "page_type": "concept", "chunk_indices": [0]},
        {"slug": "concept/rag-b", "title": "ＲＡＧ (检索增强生成)", "page_type": "concept", "chunk_indices": [1]},
    ]
    existing = [{
        "slug": "concept/rag", "title": "RAG（检索增强生成）", "summary": "规范页",
        "page_type": "concept", "status": "published", "edit_source": "user", "folder_path": "AI/检索",
    }]

    result = await service._deduplicate(candidates, existing)

    assert len(result) == 1
    assert result[0]["slug"] == "concept/rag"
    assert result[0]["title"] == "RAG（检索增强生成）"
    assert result[0]["folder"] == "AI/检索"
    assert result[0]["chunk_indices"] == [0, 1]
