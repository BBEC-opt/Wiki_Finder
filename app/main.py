from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import get_settings
from app.core.errors import AppError
from app.infrastructure.database import create_database
from app.infrastructure.providers import create_providers
from app.infrastructure.tracing import Tracing
from app.infrastructure.repository import Repository
from app.infrastructure.storage import create_storage
from app.infrastructure.vector_index import create_vector_index
from app.infrastructure.task_queue import create_task_queue
from app.parsing import ParsingService
from app.services.ingestion import IngestionService, IngestionWorker
from app.services.rag import RagService
from app.services.retrieval import RetrievalService
from app.services.outbox import OutboxDispatcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = getattr(app.state, "settings_override", None) or get_settings()
    settings.ensure_directories()
    database = create_database(settings)
    await database.initialize()
    vector_index = create_vector_index(settings)
    if vector_index:
        await vector_index.initialize()
    repo = Repository(database, vector_outbox=vector_index is not None)
    storage = create_storage(settings)
    await storage.initialize()
    parser = ParsingService(settings.artifact_dir, settings.ocr_engine)
    tracing = Tracing.from_settings(settings)
    embedding, chat = create_providers(settings)
    embedding.tracing = chat.tracing = tracing
    queue = await create_task_queue(settings)
    ingestion = IngestionService(repo, storage, queue, settings)
    worker = IngestionWorker(repo, parser, embedding, queue, settings, storage=storage, chat_provider=chat)
    retrieval = RetrievalService(repo, embedding, vector_index)
    rag = RagService(repo, retrieval, chat, settings.max_context_chars)
    worker.tracing = tracing
    retrieval.tracing = rag.tracing = tracing
    outbox = OutboxDispatcher(repo, vector_index)
    for key, value in locals().copy().items():
        if key in {"settings", "repo", "storage", "parser", "ingestion", "worker", "retrieval", "rag", "outbox"}:
            setattr(app.state, key, value)
    if settings.run_embedded_worker:
        await worker.start()
    await outbox.start()
    try:
        yield
    finally:
        await outbox.stop()
        if settings.run_embedded_worker:
            await worker.stop()
        if vector_index:
            await vector_index.close()
        await storage.close()
        await queue.close()
        await database.close()
        await tracing.close()


def create_app(settings_override=None) -> FastAPI:
    application = FastAPI(title="My Wiki RAG Demo", version="0.1.0", lifespan=lifespan)
    configured_settings = settings_override or get_settings()
    if settings_override is not None:
        application.state.settings_override = settings_override
    if configured_settings.cors_origin_list:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=configured_settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    application.include_router(router)
    application.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "frontend", html=True), name="frontend")
    application.add_exception_handler(AppError, app_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(Exception, unexpected_error_handler)
    return application


async def app_error_handler(_: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


async def validation_error_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": {
                "code": "validation_error",
                "message": "请求参数校验失败",
                "details": {"errors": jsonable_encoder(exc.errors())},
            },
        },
    )


async def unexpected_error_handler(_: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": {"code": "internal_error", "message": "服务内部错误", "details": {}}},
    )


app = create_app()
