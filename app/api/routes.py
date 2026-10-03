import json
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from app.core.errors import AppError
from app.api.schemas import (
    ArtifactResponse, ChatRequest, ChunkListResponse, ClearedResponse, DeletedResponse, HealthResponse,
    ErrorResponse,
    KnowledgeBaseCreate, KnowledgeBaseListResponse, KnowledgeBaseResponse,
    KnowledgeListResponse, KnowledgeResponse, ManualKnowledgeCreate, MessageListResponse,
    ParserPreviewResponse, SearchRequest, SearchResponse, StageListResponse,
    WikiFolderCreate, WikiFolderListResponse, WikiPageListResponse, WikiPageResponse, WikiPageWrite,
)


router = APIRouter(
    prefix="/api/v1",
    responses={
        400: {"model": ErrorResponse, "description": "请求无效"},
        404: {"model": ErrorResponse, "description": "资源不存在"},
        409: {"model": ErrorResponse, "description": "资源状态冲突"},
        413: {"model": ErrorResponse, "description": "上传内容过大"},
        422: {"model": ErrorResponse, "description": "请求参数校验失败"},
        500: {"model": ErrorResponse, "description": "服务内部错误"},
    },
)


def ok(data):
    return {"success": True, "data": data}


@router.get("/health/live", response_model=HealthResponse)
async def live():
    return ok({"status": "live"})


@router.get("/health/ready", response_model=HealthResponse)
async def ready(request: Request):
    await request.app.state.repo.query_one("SELECT 1 value")
    return ok({"status": "ready"})


@router.post("/knowledge-bases", status_code=201, response_model=KnowledgeBaseResponse)
async def create_kb(body: KnowledgeBaseCreate, request: Request):
    settings = request.app.state.settings
    chunk_size = body.chunk_size or settings.chunk_size
    chunk_overlap = body.chunk_overlap if body.chunk_overlap is not None else settings.chunk_overlap
    parent_chunk_size = body.parent_chunk_size or settings.parent_chunk_size
    if chunk_overlap >= chunk_size:
        raise AppError("invalid_chunk_config", "chunk_overlap 必须小于 chunk_size", 422)
    if parent_chunk_size < chunk_size:
        raise AppError("invalid_chunk_config", "parent_chunk_size 必须大于等于 chunk_size", 422)
    data = body.model_dump()
    data.update({
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "parent_chunk_size": parent_chunk_size,
        "embedding_provider": settings.provider_mode,
        "embedding_model": settings.embedding_model if settings.provider_mode == "openai" else "hash-embedding",
        "embedding_dimension": settings.embedding_dimension,
    })
    return ok(await request.app.state.repo.create_kb(data))


@router.get("/knowledge-bases", response_model=KnowledgeBaseListResponse)
async def list_kbs(request: Request):
    return ok(await request.app.state.repo.list_kbs())


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_kb(kb_id: str, request: Request):
    data = await request.app.state.repo.get_kb(kb_id)
    if not data:
        raise AppError("knowledge_base_not_found", "知识库不存在", 404)
    return ok(data)


@router.post("/knowledge-bases/{kb_id}/knowledge/file", status_code=202, response_model=KnowledgeResponse)
async def upload_knowledge(kb_id: str, request: Request, file: UploadFile = File(...)):
    return ok(await request.app.state.ingestion.create_file(kb_id, file))


@router.post("/knowledge-bases/{kb_id}/knowledge/manual", status_code=202, response_model=KnowledgeResponse)
async def manual_knowledge(kb_id: str, body: ManualKnowledgeCreate, request: Request):
    return ok(await request.app.state.ingestion.create_manual(kb_id, body.title, body.content))


@router.get("/knowledge-bases/{kb_id}/knowledge", response_model=KnowledgeListResponse)
async def list_knowledge(kb_id: str, request: Request):
    if not await request.app.state.repo.get_kb(kb_id):
        raise AppError("knowledge_base_not_found", "知识库不存在", 404)
    return ok(await request.app.state.repo.list_knowledge(kb_id))


@router.get("/knowledge-bases/{kb_id}/wiki/pages", response_model=WikiPageListResponse)
async def list_wiki_pages(kb_id: str, request: Request, knowledge_id: str | None = None, q: str = ""):
    if not await request.app.state.repo.get_kb(kb_id):
        raise AppError("knowledge_base_not_found", "知识库不存在", 404)
    if knowledge_id:
        knowledge = await request.app.state.repo.get_knowledge(knowledge_id)
        if not knowledge or knowledge["knowledge_base_id"] != kb_id:
            raise AppError("knowledge_not_found", "知识不存在", 404)
    return ok(await request.app.state.repo.list_wiki_pages(kb_id, knowledge_id, q.strip()))


@router.post("/knowledge-bases/{kb_id}/wiki/pages", status_code=201, response_model=WikiPageResponse)
async def create_wiki_page(kb_id: str, body: WikiPageWrite, request: Request):
    if not await request.app.state.repo.get_kb(kb_id):
        raise AppError("knowledge_base_not_found", "知识库不存在", 404)
    if await request.app.state.repo.get_wiki_page_by_slug(kb_id, body.slug):
        raise AppError("wiki_slug_conflict", "Wiki slug 已存在", 409)
    page = await request.app.state.repo.save_wiki_page(kb_id, body.model_dump())
    await request.app.state.repo.finalize_wiki(kb_id)
    return ok(page)


@router.get("/knowledge-bases/{kb_id}/wiki/pages/by-slug/{slug:path}", response_model=WikiPageResponse)
async def get_wiki_page_by_slug(kb_id: str, slug: str, request: Request):
    page = await request.app.state.repo.get_wiki_page_by_slug(kb_id, slug)
    if not page:
        raise AppError("wiki_page_not_found", "Wiki 页面不存在", 404)
    return ok(page)


@router.put("/knowledge-bases/{kb_id}/wiki/pages/{page_id}", response_model=WikiPageResponse)
async def update_wiki_page(kb_id: str, page_id: str, body: WikiPageWrite, request: Request):
    page = await request.app.state.repo.get_wiki_page(page_id)
    if not page or page["knowledge_base_id"] != kb_id:
        raise AppError("wiki_page_not_found", "Wiki 页面不存在", 404)
    conflict = await request.app.state.repo.get_wiki_page_by_slug(kb_id, body.slug)
    if conflict and conflict["id"] != page_id:
        raise AppError("wiki_slug_conflict", "Wiki slug 已存在", 409)
    page = await request.app.state.repo.save_wiki_page(kb_id, body.model_dump(), page_id)
    await request.app.state.repo.finalize_wiki(kb_id)
    return ok(page)


@router.delete("/knowledge-bases/{kb_id}/wiki/pages/{page_id}", response_model=DeletedResponse)
async def delete_wiki_page(kb_id: str, page_id: str, request: Request):
    page = await request.app.state.repo.get_wiki_page(page_id)
    if not page or page["knowledge_base_id"] != kb_id:
        raise AppError("wiki_page_not_found", "Wiki 页面不存在", 404)
    await request.app.state.repo.execute("DELETE FROM wiki_pages WHERE id=?", (page_id,))
    await request.app.state.repo.finalize_wiki(kb_id)
    return ok({"deleted": True})


@router.get("/knowledge-bases/{kb_id}/wiki/pages/{page_id}/sources")
async def wiki_page_sources(kb_id: str, page_id: str, request: Request):
    page = await request.app.state.repo.get_wiki_page(page_id)
    if not page or page["knowledge_base_id"] != kb_id:
        raise AppError("wiki_page_not_found", "Wiki 页面不存在", 404)
    return ok(await request.app.state.repo.wiki_page_sources(page_id))


@router.get("/knowledge-bases/{kb_id}/wiki/pages/{page_id}/backlinks")
async def wiki_page_backlinks(kb_id: str, page_id: str, request: Request):
    page = await request.app.state.repo.get_wiki_page(page_id)
    if not page or page["knowledge_base_id"] != kb_id:
        raise AppError("wiki_page_not_found", "Wiki 页面不存在", 404)
    return ok(await request.app.state.repo.wiki_page_backlinks(page_id))


@router.get("/knowledge-bases/{kb_id}/wiki/folders", response_model=WikiFolderListResponse)
async def list_wiki_folders(kb_id: str, request: Request):
    if not await request.app.state.repo.get_kb(kb_id):
        raise AppError("knowledge_base_not_found", "知识库不存在", 404)
    return ok(await request.app.state.repo.list_wiki_folders(kb_id))


@router.get("/knowledge-bases/{kb_id}/wiki/stats")
async def wiki_stats(kb_id: str, request: Request):
    if not await request.app.state.repo.get_kb(kb_id):
        raise AppError("knowledge_base_not_found", "知识库不存在", 404)
    return ok(await request.app.state.repo.wiki_stats(kb_id))


@router.get("/knowledge-bases/{kb_id}/wiki/lint")
async def wiki_lint(kb_id: str, request: Request):
    if not await request.app.state.repo.get_kb(kb_id):
        raise AppError("knowledge_base_not_found", "知识库不存在", 404)
    return ok(await request.app.state.repo.wiki_lint(kb_id))


@router.post("/knowledge-bases/{kb_id}/wiki/folders", status_code=201)
async def create_wiki_folder(kb_id: str, body: WikiFolderCreate, request: Request):
    if not await request.app.state.repo.get_kb(kb_id):
        raise AppError("knowledge_base_not_found", "知识库不存在", 404)
    return ok(await request.app.state.repo.create_wiki_folder(kb_id, body.name, body.parent_id))


@router.get("/wiki/pages/{page_id}", response_model=WikiPageResponse)
async def get_wiki_page(page_id: str, request: Request):
    page = await request.app.state.repo.get_wiki_page(page_id)
    if not page:
        raise AppError("wiki_page_not_found", "Wiki 页面不存在", 404)
    return ok(page)


@router.delete("/knowledge-bases/{kb_id}/knowledge", response_model=ClearedResponse)
async def clear_knowledge(kb_id: str, request: Request):
    if not await request.app.state.repo.get_kb(kb_id):
        raise AppError("knowledge_base_not_found", "知识库不存在", 404)
    knowledge = await request.app.state.repo.list_knowledge(kb_id)
    if any(item["parse_status"] in {"pending", "processing"} for item in knowledge):
        raise AppError("processing_in_progress", "知识库中仍有资料正在处理，请稍后再清理", 409)
    knowledge_ids = [item["id"] for item in knowledge]
    deleted_count = await request.app.state.repo.clear_knowledge_base(kb_id)
    await request.app.state.storage.delete_knowledge_base(kb_id, knowledge_ids)
    return ok({"cleared": True, "deleted_count": deleted_count})


@router.get("/knowledge/{knowledge_id}", response_model=KnowledgeResponse)
async def get_knowledge(knowledge_id: str, request: Request):
    data = await request.app.state.repo.get_knowledge(knowledge_id)
    if not data:
        raise AppError("knowledge_not_found", "知识不存在", 404)
    return ok(data)


@router.get("/knowledge/{knowledge_id}/chunks", response_model=ChunkListResponse)
async def get_chunks(knowledge_id: str, request: Request):
    if not await request.app.state.repo.get_knowledge(knowledge_id):
        raise AppError("knowledge_not_found", "知识不存在", 404)
    return ok(await request.app.state.repo.list_chunks(knowledge_id))


@router.get("/knowledge/{knowledge_id}/stages", response_model=StageListResponse)
async def get_stages(knowledge_id: str, request: Request):
    if not await request.app.state.repo.get_knowledge(knowledge_id):
        raise AppError("knowledge_not_found", "知识不存在", 404)
    return ok(await request.app.state.repo.list_stages(knowledge_id))


@router.get("/knowledge/{knowledge_id}/wiki-builds")
async def wiki_builds(knowledge_id: str, request: Request):
    if not await request.app.state.repo.get_knowledge(knowledge_id):
        raise AppError("knowledge_not_found", "知识不存在", 404)
    return ok(await request.app.state.repo.list_wiki_builds(knowledge_id))


@router.get("/wiki/builds/{build_id}")
async def wiki_build_detail(build_id: str, request: Request):
    build = await request.app.state.repo.wiki_build_details(build_id)
    if not build:
        raise AppError("wiki_build_not_found", "Wiki 构建批次不存在", 404)
    return ok(build)


@router.get("/knowledge/{knowledge_id}/parsed", response_class=PlainTextResponse)
async def get_parsed(knowledge_id: str, request: Request):
    if not await request.app.state.repo.get_knowledge(knowledge_id):
        raise AppError("knowledge_not_found", "知识不存在", 404)
    await request.app.state.storage.ensure_artifacts(knowledge_id)
    path = request.app.state.storage.artifact_dir(knowledge_id) / "parsed.md"
    if not path.exists():
        raise AppError("parsed_content_not_found", "解析产物尚不存在", 404)
    return path.read_text(encoding="utf-8")


@router.get("/knowledge/{knowledge_id}/artifacts", response_model=ArtifactResponse)
async def get_artifacts(knowledge_id: str, request: Request):
    if not await request.app.state.repo.get_knowledge(knowledge_id):
        raise AppError("knowledge_not_found", "知识不存在", 404)
    await request.app.state.storage.ensure_artifacts(knowledge_id)
    path = request.app.state.storage.artifact_dir(knowledge_id) / "manifest.json"
    if not path.exists():
        raise AppError("artifact_not_found", "解析产物尚不存在", 404)
    return ok(json.loads(path.read_text(encoding="utf-8")))


@router.get("/artifacts/{knowledge_id}/{image_name}")
async def get_artifact(knowledge_id: str, image_name: str, request: Request):
    await request.app.state.storage.ensure_artifacts(knowledge_id)
    root = request.app.state.storage.artifact_dir(knowledge_id) / "images"
    path = (root / Path(image_name).name).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise AppError("artifact_not_found", "资源不存在", 404)
    return FileResponse(path)


@router.get("/artifacts/{knowledge_id}/images/by-id/{image_id}")
async def get_artifact_by_id(knowledge_id: str, image_id: str, request: Request):
    if not await request.app.state.repo.get_knowledge(knowledge_id):
        raise AppError("knowledge_not_found", "知识不存在", 404)
    await request.app.state.storage.ensure_artifacts(knowledge_id)
    artifact_root = request.app.state.storage.artifact_dir(knowledge_id)
    manifest_path = artifact_root / "manifest.json"
    if not manifest_path.exists():
        raise AppError("artifact_not_found", "资源不存在", 404)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image = next((x for x in manifest.get("images", []) if x.get("id") == image_id), None)
    if not image:
        raise AppError("artifact_not_found", "资源不存在", 404)
    path = Path(image["storage_path"]).resolve()
    if artifact_root.resolve() not in path.parents or not path.is_file():
        raise AppError("artifact_not_found", "资源不存在", 404)
    return FileResponse(path)


@router.post("/knowledge/{knowledge_id}/reprocess", status_code=202, response_model=KnowledgeResponse)
async def reprocess(knowledge_id: str, request: Request):
    return ok(await request.app.state.ingestion.reprocess(knowledge_id))


@router.delete("/knowledge/{knowledge_id}", response_model=DeletedResponse)
async def delete_knowledge(knowledge_id: str, request: Request):
    knowledge = await request.app.state.repo.get_knowledge(knowledge_id)
    if not knowledge:
        raise AppError("knowledge_not_found", "知识不存在", 404)
    await request.app.state.repo.delete_knowledge(knowledge_id)
    await request.app.state.storage.delete_knowledge(knowledge["knowledge_base_id"], knowledge_id)
    return ok({"deleted": True})


@router.post("/parser/preview", response_model=ParserPreviewResponse)
async def parser_preview(request: Request, file: UploadFile = File(...)):
    import tempfile
    suffix = Path(file.filename or "preview").suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        temp = Path(handle.name)
        while block := await file.read(1024 * 1024):
            handle.write(block)
    preview_id = "preview-" + temp.stem
    try:
        parsed = await request.app.state.parser.parse(temp, preview_id, file.filename or "")
        return ok({"markdown": parsed.markdown, "manifest": parsed.manifest()})
    finally:
        temp.unlink(missing_ok=True)


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request):
    results = await request.app.state.retrieval.search(
        body.query, body.knowledge_base_ids, body.knowledge_ids, body.top_k,
        body.candidate_k, body.score_threshold,
    )
    return ok(results)


@router.post("/chat")
async def chat(body: ChatRequest, request: Request):
    return StreamingResponse(
        request.app.state.rag.stream(body), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/messages", response_model=MessageListResponse)
async def messages(session_id: str, request: Request):
    return ok(await request.app.state.repo.chat_history(session_id, 100))


@router.delete("/sessions/{session_id}/messages", response_model=DeletedResponse)
async def clear_messages(session_id: str, request: Request):
    await request.app.state.repo.clear_chat_history(session_id)
    return ok({"deleted": True})
