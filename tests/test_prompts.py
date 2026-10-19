import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app
from app.services.prompts import DEFAULT_PROMPTS, MANDATORY_CONSTRAINTS, PromptService


def test_prompt_service_defaults_overrides_and_reset(tmp_path: Path):
    path = tmp_path / "data" / "prompts.json"
    prompts = PromptService(path)

    assert prompts.get("rag_answer") == DEFAULT_PROMPTS["rag_answer"] + "\n\n" + MANDATORY_CONSTRAINTS["rag_answer"]
    assert prompts.list()[0]["is_custom"] is False

    saved = prompts.update("rag_answer", "  自定义问答规则  ")
    assert saved["content"] == "自定义问答规则"
    assert saved["is_custom"] is True
    assert prompts.get("rag_answer") == "自定义问答规则\n\n" + MANDATORY_CONSTRAINTS["rag_answer"]
    assert json.loads(path.read_text(encoding="utf-8")) == {"rag_answer": "自定义问答规则"}

    reloaded = PromptService(path)
    assert reloaded.get("rag_answer") == prompts.get("rag_answer")
    reset = reloaded.reset("rag_answer")
    assert reset["is_custom"] is False
    assert reloaded.get("rag_answer") == DEFAULT_PROMPTS["rag_answer"] + "\n\n" + MANDATORY_CONSTRAINTS["rag_answer"]
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_prompt_service_rejects_unknown_and_empty_updates(tmp_path: Path):
    prompts = PromptService(tmp_path / "prompts.json")

    with pytest.raises(AppError) as unknown:
        prompts.get("missing")
    assert getattr(unknown.value, "code", None) == "prompt_not_found"
    with pytest.raises(AppError) as empty:
        prompts.update("rag_answer", " \n ")
    assert getattr(empty.value, "code", None) == "empty_prompt"


def test_prompt_management_api_persists_resets_and_validates(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "data" / "demo.db",
        upload_dir=tmp_path / "uploads",
        artifact_dir=tmp_path / "artifacts",
        temp_dir=tmp_path / "tmp",
        embedding_dimension=64,
    )
    with TestClient(create_app(settings)) as client:
        listing = client.get("/api/v1/prompts")
        assert listing.status_code == 200
        prompts = listing.json()["data"]
        assert {prompt["id"] for prompt in prompts} == {"rag_answer", "wiki_candidates"}
        assert all(not prompt["is_custom"] for prompt in prompts)

        saved = client.put("/api/v1/prompts/wiki_candidates", json={"content": "只输出有充分证据的候选。"})
        assert saved.status_code == 200
        assert saved.json()["data"]["is_custom"] is True
        assert client.get("/api/v1/prompts").json()["data"][1]["content"] == "只输出有充分证据的候选。"
        assert (tmp_path / "data" / "prompts.json").is_file()

        empty = client.put("/api/v1/prompts/rag_answer", json={"content": "   "})
        assert empty.status_code == 422
        missing = client.put("/api/v1/prompts/unknown", json={"content": "规则"})
        assert missing.status_code == 404

        reset = client.post("/api/v1/prompts/wiki_candidates/reset")
        assert reset.status_code == 200
        assert reset.json()["data"]["is_custom"] is False
        assert client.get("/api/v1/prompts").json()["data"][1]["content"] == DEFAULT_PROMPTS["wiki_candidates"]



def test_prompt_instances_observe_external_updates_and_resets(tmp_path: Path):
    path = tmp_path / "prompts.json"
    web = PromptService(path)
    worker = PromptService(path)
    web.update("wiki_candidates", "只抽取设备相关概念")
    assert worker.get("wiki_candidates").startswith("只抽取设备相关概念")
    worker.update("rag_answer", "使用简短回答")
    assert web.get("rag_answer").startswith("使用简短回答")
    assert web.list()[1]["content"] == "只抽取设备相关概念"
    web.reset("wiki_candidates")
    assert worker.get("wiki_candidates").startswith(DEFAULT_PROMPTS["wiki_candidates"])
    assert worker.get("rag_answer").startswith("使用简短回答")


@pytest.mark.asyncio
async def test_standalone_worker_loads_and_refreshes_prompts(tmp_path: Path, monkeypatch):
    import app.worker as worker_module

    settings = Settings(
        _env_file=None, database_path=tmp_path / "demo.db",
        upload_dir=tmp_path / "uploads", artifact_dir=tmp_path / "artifacts",
        temp_dir=tmp_path / "tmp", embedding_dimension=64,
    )
    prompts = PromptService(tmp_path / "prompts.json")
    prompts.update("wiki_candidates", "启动前规则")
    started = asyncio.Event()
    workers = []

    async def capture_worker(worker):
        workers.append(worker)
        started.set()

    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module.IngestionWorker, "start", capture_worker)
    task = asyncio.create_task(worker_module.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert workers[0].wiki.prompts.get("wiki_candidates").startswith("启动前规则")
        prompts.update("wiki_candidates", "启动后规则")
        assert workers[0].wiki.prompts.get("wiki_candidates").startswith("启动后规则")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
