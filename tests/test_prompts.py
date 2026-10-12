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

