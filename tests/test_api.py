import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


SAMPLE_PDF = Path(__file__).parents[1] / "samples" / "rebuttal (7).pdf"


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "api.db",
        upload_dir=tmp_path / "uploads",
        artifact_dir=tmp_path / "artifacts",
        embedding_dimension=64,
        chunk_size=300,
        chunk_overlap=30,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def wait_for_completion(client: TestClient, knowledge_id: str, timeout: float = 8) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/knowledge/{knowledge_id}")
        assert response.status_code == 200
        knowledge = response.json()["data"]
        if knowledge["parse_status"] in {"completed", "failed"}:
            return knowledge
        time.sleep(0.05)
    raise AssertionError("知识处理未在限定时间内完成")


def create_knowledge_base(client: TestClient, **overrides) -> dict:
    body = {"name": "接口测试知识库", "description": "用于 API 测试"}
    body.update(overrides)
    response = client.post("/api/v1/knowledge-bases", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_health_and_knowledge_base_queries(client: TestClient):
    assert client.get("/api/v1/health/live").json() == {
        "success": True,
        "data": {"status": "live"},
    }
    assert client.get("/api/v1/health/ready").json() == {
        "success": True,
        "data": {"status": "ready"},
    }

    knowledge_base = create_knowledge_base(
        client, chunk_size=420, chunk_overlap=42,
    )
    assert knowledge_base["chunk_size"] == 420
    assert knowledge_base["chunk_overlap"] == 42
    assert knowledge_base["embedding_provider"] == "offline"
    assert knowledge_base["embedding_dimension"] == 64

    listed = client.get("/api/v1/knowledge-bases")
    assert listed.status_code == 200
    assert listed.json()["data"] == [knowledge_base]

    fetched = client.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"] == knowledge_base


def test_file_ingestion_resources_reprocess_and_delete(client: TestClient):
    knowledge_base = create_knowledge_base(client)
    upload = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/knowledge/file",
        files={
            "file": (
                "guide.md",
                "# 联网指南\n\n进入设置页面并选择无线网络。".encode(),
                "text/markdown",
            )
        },
    )
    assert upload.status_code == 202, upload.text
    created = upload.json()["data"]
    assert created["source_type"] == "file"
    assert created["file_name"] == "guide.md"

    knowledge = wait_for_completion(client, created["id"])
    assert knowledge["parse_status"] == "completed", knowledge

    listed = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/knowledge"
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [created["id"]]

    stages = client.get(f"/api/v1/knowledge/{created['id']}/stages")
    assert stages.status_code == 200
    assert [item["stage"] for item in stages.json()["data"]] == [
        "parsing", "chunking", "embedding", "indexing", "wiki_generation",
    ]
    assert all(item["status"] == "completed" for item in stages.json()["data"])

    artifacts = client.get(f"/api/v1/knowledge/{created['id']}/artifacts")
    assert artifacts.status_code == 200
    assert artifacts.json()["data"]["parser_engine"] == "builtin"

    duplicate = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/knowledge/file",
        files={"file": ("copy.md", "# 联网指南\n\n进入设置页面并选择无线网络。".encode())},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_document"

    reprocessed = client.post(f"/api/v1/knowledge/{created['id']}/reprocess")
    assert reprocessed.status_code == 202
    assert wait_for_completion(client, created["id"])["parse_status"] == "completed"

    builds = client.get(f"/api/v1/knowledge/{created['id']}/wiki-builds").json()["data"]
    assert builds[0]["status"] == "degraded"
    detail = client.get(f"/api/v1/wiki/builds/{builds[0]['id']}").json()["data"]
    assert [stage["stage"] for stage in detail["stages"]] == [
        "candidate_extraction", "citation_mapping", "deduplication", "taxonomy",
        "reduce", "persist", "finalize", "quality",
    ]
    assert all(stage["status"] == "completed" for stage in detail["stages"])

    deleted = client.delete(f"/api/v1/knowledge/{created['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True, "data": {"deleted": True}}
    assert client.get(f"/api/v1/knowledge/{created['id']}").status_code == 404


def test_pdf_upload_parse_chunk_and_search(client: TestClient):
    assert SAMPLE_PDF.is_file(), f"缺少 PDF 测试样例：{SAMPLE_PDF}"
    knowledge_base = create_knowledge_base(client, name="PDF 上传测试")

    with SAMPLE_PDF.open("rb") as pdf:
        response = client.post(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/knowledge/file",
            files={"file": (SAMPLE_PDF.name, pdf, "application/pdf")},
        )

    assert response.status_code == 202, response.text
    created = response.json()["data"]
    assert created["source_type"] == "file"
    assert created["file_name"] == SAMPLE_PDF.name

    knowledge = wait_for_completion(client, created["id"])
    assert knowledge["parse_status"] == "completed", knowledge
    assert knowledge["parser_engine"] == "builtin"
    assert knowledge["chunk_count"] > 1

    parsed = client.get(f"/api/v1/knowledge/{created['id']}/parsed")
    assert parsed.status_code == 200
    assert "dual-factor model" in parsed.text
    assert "<!-- page:1 -->" in parsed.text

    chunks = client.get(f"/api/v1/knowledge/{created['id']}/chunks")
    assert chunks.status_code == 200
    assert len(chunks.json()["data"]) == knowledge["chunk_count"]
    assert all(chunk["page_number"] == 1 for chunk in chunks.json()["data"])
    assert all(chunk["parent_id"] for chunk in chunks.json()["data"])
    assert all(chunk["parent_content"] for chunk in chunks.json()["data"])

    stages = client.get(f"/api/v1/knowledge/{created['id']}/stages")
    assert stages.status_code == 200
    assert [stage["stage"] for stage in stages.json()["data"]] == [
        "parsing", "chunking", "embedding", "indexing", "wiki_generation",
    ]
    assert all(stage["status"] == "completed" for stage in stages.json()["data"])

    wiki = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages",
        params={"knowledge_id": created["id"]},
    )
    assert wiki.status_code == 200
    pages = wiki.json()["data"]
    assert [page["page_type"] for page in pages] == ["summary", "source"]
    assert "## 核心内容" in pages[0]["content"]
    assert pages[0]["source_refs"][0]["knowledge_id"] == created["id"]
    assert pages[1]["content"].count("artifact://") == 3

    page = client.get(f"/api/v1/wiki/pages/{pages[0]['id']}")
    assert page.status_code == 200
    assert page.json()["data"] == pages[0]

    search = client.post(
        "/api/v1/search",
        json={
            "query": "dual-factor model",
            "knowledge_base_ids": [knowledge_base["id"]],
            "knowledge_ids": [created["id"]],
            "top_k": 3,
        },
    )
    assert search.status_code == 200, search.text
    results = search.json()["data"]
    assert results
    assert any("dual-factor model" in item["content"] for item in results)


def test_parser_preview_does_not_create_knowledge(client: TestClient):
    knowledge_base = create_knowledge_base(client)
    preview = client.post(
        "/api/v1/parser/preview",
        files={"file": ("preview.md", b"# Preview\n\nSome content.", "text/markdown")},
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()["data"]
    assert "# Preview" in data["markdown"]
    assert data["manifest"]["parser_engine"] == "builtin"

    listed = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/knowledge"
    )
    assert listed.json()["data"] == []


def test_wiki_page_crud_search_and_sources(client: TestClient):
    knowledge_base = create_knowledge_base(client)
    created = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/knowledge/manual",
        json={"title": "检索手册", "content": "# 检索设计\n\n混合检索同时使用稠密向量和关键词证据。"},
    ).json()["data"]
    assert wait_for_completion(client, created["id"])["parse_status"] == "completed"

    pages = client.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages").json()["data"]
    assert pages
    assert pages[0]["page_type"] == "index"
    stats = client.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/stats")
    assert stats.status_code == 200
    assert stats.json()["data"]["total_pages"] == len(pages)
    assert client.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/lint").status_code == 200
    sourced_page = next(page for page in pages if page["page_type"] == "summary")
    sources = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages/{sourced_page['id']}/sources"
    )
    assert sources.status_code == 200
    assert sources.json()["data"][0]["knowledge_id"] == created["id"]
    assert sources.json()["data"][0]["chunk_id"]

    protected = sourced_page
    protected_body = {
        "slug": protected["slug"], "title": protected["title"], "summary": protected["summary"],
        "content": protected["content"] + "\n\n人工校订。", "page_type": "topic", "status": "published",
    }
    changed = client.put(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages/{protected['id']}", json=protected_body
    )
    assert changed.status_code == 200
    assert client.post(f"/api/v1/knowledge/{created['id']}/reprocess").status_code == 202
    assert wait_for_completion(client, created["id"])["parse_status"] == "completed"
    preserved = client.get(f"/api/v1/wiki/pages/{protected['id']}").json()["data"]
    assert "人工校订" in preserved["content"]
    assert preserved["edit_source"] == "user"

    body = {
        "slug": "concept/hybrid-search", "title": "混合检索", "summary": "人工维护的概念页",
        "content": "# 混合检索\n\n这是人工整理的内容。", "page_type": "concept", "status": "published",
    }
    manual = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages", json=body
    )
    assert manual.status_code == 201, manual.text
    page = manual.json()["data"]
    assert page["edit_source"] == "user"

    conflict = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages", json=body
    )
    assert conflict.status_code == 409

    found = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages", params={"q": "人工维护"}
    ).json()["data"]
    assert page["id"] in {item["id"] for item in found}
    assert any(item["page_type"] == "index" for item in found)

    body["content"] += "\n\n补充内容。"
    updated = client.put(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages/{page['id']}", json=body
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["version"] == 2

    linking_body = {
        "slug": "concept/retrieval-notes", "title": "检索笔记", "summary": "链接测试",
        "content": "参考 [[concept/hybrid-search|混合检索]]。", "page_type": "concept", "status": "published",
    }
    linking = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages", json=linking_body
    )
    assert linking.status_code == 201
    backlinks = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages/{page['id']}/backlinks"
    ).json()["data"]
    assert "concept/retrieval-notes" in {item["slug"] for item in backlinks}
    assert "index/home" in {item["slug"] for item in backlinks}

    deleted = client.delete(f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages/{page['id']}")
    assert deleted.json()["data"] == {"deleted": True}
    refreshed_pages = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages"
    ).json()["data"]
    index_page = next(item for item in refreshed_pages if item["slug"] == "index/home")
    assert "[[concept/hybrid-search|混合检索]]" not in index_page["content"]


def test_shared_wiki_page_is_rebuilt_when_one_source_is_deleted(client: TestClient):
    knowledge_base = create_knowledge_base(client, name="共享页面维护")
    first = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/knowledge/manual",
        json={"title": "来源甲", "content": "# 共享主题\n\n甲来源独有事实。" + "这是甲来源的有效说明。" * 8},
    ).json()["data"]
    second = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/knowledge/manual",
        json={"title": "来源乙", "content": "# 共享主题\n\n乙来源独有事实。" + "这是乙来源的有效说明。" * 8},
    ).json()["data"]
    assert wait_for_completion(client, first["id"])["parse_status"] == "completed"
    assert wait_for_completion(client, second["id"])["parse_status"] == "completed"

    pages = client.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages").json()["data"]
    shared = next(page for page in pages if page["page_type"] == "concept" and page["title"] == "共享主题")
    before = client.get(f"/api/v1/wiki/pages/{shared['id']}").json()["data"]
    assert "甲来源独有事实" in before["content"]
    assert "乙来源独有事实" in before["content"]

    assert client.delete(f"/api/v1/knowledge/{second['id']}").status_code == 200
    after = client.get(f"/api/v1/wiki/pages/{shared['id']}").json()["data"]
    assert "甲来源独有事实" in after["content"]
    assert "乙来源独有事实" not in after["content"]
    sources = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/wiki/pages/{shared['id']}/sources"
    ).json()["data"]
    assert {source["knowledge_id"] for source in sources} == {first["id"]}


@pytest.mark.parametrize(
    ("path", "error_code"),
    [
        ("/api/v1/knowledge-bases/missing", "knowledge_base_not_found"),
        ("/api/v1/knowledge/missing", "knowledge_not_found"),
        ("/api/v1/knowledge/missing/chunks", "knowledge_not_found"),
        ("/api/v1/knowledge/missing/stages", "knowledge_not_found"),
        ("/api/v1/knowledge/missing/artifacts", "knowledge_not_found"),
    ],
)
def test_missing_resources_use_unified_error_contract(
    client: TestClient, path: str, error_code: str,
):
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "error": {"code": error_code, "message": response.json()["error"]["message"], "details": {}},
    }


@pytest.mark.parametrize(
    "body",
    [
        {"query": "", "knowledge_base_ids": ["kb"]},
        {"query": "test", "knowledge_base_ids": []},
        {"query": "test", "knowledge_base_ids": ["kb"], "top_k": 0},
        {"query": "test", "knowledge_base_ids": ["kb"], "top_k": 10, "candidate_k": 5},
        {"query": "test", "knowledge_base_ids": ["kb"], "score_threshold": 2},
    ],
)
def test_search_rejects_invalid_requests(client: TestClient, body: dict):
    response = client.post("/api/v1/search", json=body)
    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["errors"]


def test_mutating_missing_resources_return_not_found(client: TestClient):
    manual = client.post(
        "/api/v1/knowledge-bases/missing/knowledge/manual",
        json={"title": "missing", "content": "content"},
    )
    assert manual.status_code == 404
    assert manual.json()["error"]["code"] == "knowledge_base_not_found"

    upload = client.post(
        "/api/v1/knowledge-bases/missing/knowledge/file",
        files={"file": ("test.md", b"content")},
    )
    assert upload.status_code == 404
    assert upload.json()["error"]["code"] == "knowledge_base_not_found"

    reprocess = client.post("/api/v1/knowledge/missing/reprocess")
    assert reprocess.status_code == 404
    assert reprocess.json()["error"]["code"] == "knowledge_not_found"

    deleted = client.delete("/api/v1/knowledge/missing")
    assert deleted.status_code == 404
    assert deleted.json()["error"]["code"] == "knowledge_not_found"
