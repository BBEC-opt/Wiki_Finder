import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def wait_completed(client, knowledge_id, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/v1/knowledge/{knowledge_id}").json()["data"]
        if data["parse_status"] in {"completed", "failed"}:
            return data
        time.sleep(0.05)
    raise AssertionError("ingestion did not finish")


def test_full_ingestion_search_and_chat(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "demo.db", upload_dir=tmp_path / "uploads",
        artifact_dir=tmp_path / "artifacts", embedding_dimension=64,
        chunk_size=300, chunk_overlap=30,
    )
    with TestClient(create_app(settings)) as client:
        kb = client.post("/api/v1/knowledge-bases", json={"name": "产品知识库"}).json()["data"]
        response = client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/knowledge/manual",
            json={"title": "设备手册", "content": "# 恢复出厂设置\n\n长按复位按钮十秒，状态灯闪烁三次后松开。"},
        )
        assert response.status_code == 202
        knowledge = wait_completed(client, response.json()["data"]["id"])
        assert knowledge["parse_status"] == "completed", knowledge
        assert knowledge["chunk_count"] >= 1

        parsed = client.get(f"/api/v1/knowledge/{knowledge['id']}/parsed")
        assert parsed.status_code == 200
        assert "恢复出厂设置" in parsed.text
        chunks = client.get(f"/api/v1/knowledge/{knowledge['id']}/chunks")
        assert chunks.status_code == 200
        assert chunks.json()["data"][0]["content"]

        search = client.post("/api/v1/search", json={
            "query": "怎么恢复出厂设置", "knowledge_base_ids": [kb["id"]], "top_k": 3,
        })
        assert search.status_code == 200, search.text
        assert "复位按钮" in search.json()["data"][0]["content"]

        chat = client.post("/api/v1/chat", json={
            "session_id": "s1", "query": "怎么恢复出厂设置", "knowledge_base_ids": [kb["id"]],
        })
        assert chat.status_code == 200
        assert "event: references" in chat.text
        assert "event: done" in chat.text
        assert "复位按钮" in chat.text


def test_frontend_and_openapi_contract(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "demo.db", upload_dir=tmp_path / "uploads",
        artifact_dir=tmp_path / "artifacts", embedding_dimension=64,
    )
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "知识工作台" in page.text
        assert 'type="button" class="close"' in page.text
        assert 'rel="icon"' in page.text
        assert 'id="viewerDialog"' in page.text
        script = client.get("/app.js")
        assert script.status_code == 200
        assert "renderMarkdown(page.content,page.knowledge_id)" in script.text
        assert "/images/by-id/" in script.text

        schema = client.get("/openapi.json").json()
        create = schema["paths"]["/api/v1/knowledge-bases"]["post"]
        assert create["responses"]["201"]["content"]["application/json"]["schema"]
        search = schema["paths"]["/api/v1/search"]["post"]
        assert search["responses"]["200"]["content"]["application/json"]["schema"]

        invalid = client.post("/api/v1/knowledge-bases", json={"name": ""})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "validation_error"
        validation_schema = create["responses"]["422"]["content"]["application/json"]["schema"]
        assert validation_schema["$ref"].endswith("/ErrorResponse")


def test_api_error_contract_upload_and_cors(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "demo.db", upload_dir=tmp_path / "uploads",
        artifact_dir=tmp_path / "artifacts", embedding_dimension=64,
        max_upload_mb=1, cors_origins="http://localhost:5173",
    )
    with TestClient(create_app(settings)) as client:
        missing = client.get("/api/v1/knowledge-bases/missing/knowledge")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "knowledge_base_not_found"
        missing_parsed = client.get("/api/v1/knowledge/missing/parsed")
        assert missing_parsed.status_code == 404
        assert missing_parsed.json()["error"]["code"] == "knowledge_not_found"
        missing_image = client.get("/api/v1/artifacts/missing/images/by-id/image")
        assert missing_image.status_code == 404
        assert missing_image.json()["error"]["code"] == "knowledge_not_found"

        invalid_chunks = client.post(
            "/api/v1/knowledge-bases", json={"name": "bad", "chunk_overlap": 900},
        )
        assert invalid_chunks.status_code == 422
        assert invalid_chunks.json()["error"]["code"] == "invalid_chunk_config"

        kb = client.post("/api/v1/knowledge-bases", json={"name": "uploads"}).json()["data"]
        empty = client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/knowledge/file",
            files={"file": ("empty.md", b"", "text/markdown")},
        )
        assert empty.status_code == 422
        assert empty.json()["error"]["code"] == "empty_file"

        too_large = client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/knowledge/file",
            files={"file": ("large.md", b"x" * (1024 * 1024 + 1), "text/markdown")},
        )
        assert too_large.status_code == 413
        assert too_large.json()["error"]["code"] == "file_too_large"

        preflight = client.options(
            "/api/v1/knowledge-bases",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_chat_history_can_be_loaded_and_cleared(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "demo.db", upload_dir=tmp_path / "uploads",
        artifact_dir=tmp_path / "artifacts", embedding_dimension=64,
    )
    with TestClient(create_app(settings)) as client:
        kb = client.post("/api/v1/knowledge-bases", json={"name": "chat"}).json()["data"]
        response = client.post("/api/v1/chat", json={
            "session_id": "session-to-clear", "query": "没有资料时如何回答？",
            "knowledge_base_ids": [kb["id"]],
        })
        assert response.status_code == 200
        assert "event: done" in response.text

        history = client.get("/api/v1/sessions/session-to-clear/messages")
        assert [item["role"] for item in history.json()["data"]] == ["user", "assistant"]

        cleared = client.delete("/api/v1/sessions/session-to-clear/messages")
        assert cleared.status_code == 200
        assert cleared.json()["data"] == {"deleted": True}
        assert client.get("/api/v1/sessions/session-to-clear/messages").json()["data"] == []


def test_chat_stream_reports_retrieval_errors_as_sse(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "demo.db", upload_dir=tmp_path / "uploads",
        artifact_dir=tmp_path / "artifacts", embedding_dimension=64,
    )
    with TestClient(create_app(settings)) as client:
        kb = client.post("/api/v1/knowledge-bases", json={"name": "broken-chat"}).json()["data"]

        async def fail_search(*_args, **_kwargs):
            raise RuntimeError("retrieval unavailable")

        client.app.state.retrieval.search = fail_search
        response = client.post("/api/v1/chat", json={
            "session_id": "broken-session", "query": "test",
            "knowledge_base_ids": [kb["id"]],
        })
        assert response.status_code == 200
        assert "event: error" in response.text
        assert "retrieval unavailable" in response.text


def test_clear_knowledge_base_keeps_base_and_removes_contents(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "demo.db", upload_dir=tmp_path / "uploads",
        artifact_dir=tmp_path / "artifacts", embedding_dimension=64,
    )
    with TestClient(create_app(settings)) as client:
        kb = client.post("/api/v1/knowledge-bases", json={"name": "clear-me"}).json()["data"]
        created = client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/knowledge/manual",
            json={"title": "temporary", "content": "这是一份等待清理的资料。"},
        ).json()["data"]
        wait_completed(client, created["id"])

        cleared = client.delete(f"/api/v1/knowledge-bases/{kb['id']}/knowledge")
        assert cleared.status_code == 200
        assert cleared.json()["data"] == {"cleared": True, "deleted_count": 1}
        assert client.get(f"/api/v1/knowledge-bases/{kb['id']}").status_code == 200
        assert client.get(f"/api/v1/knowledge-bases/{kb['id']}/knowledge").json()["data"] == []
        assert client.get(f"/api/v1/knowledge/{created['id']}").status_code == 404
