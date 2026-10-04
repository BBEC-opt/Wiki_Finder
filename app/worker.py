"""独立摄取 Worker：`python -m app.worker`。"""

import asyncio

from app.core.config import get_settings
from app.infrastructure.database import create_database
from app.infrastructure.providers import create_providers
from app.infrastructure.tracing import Tracing
from app.infrastructure.repository import Repository
from app.infrastructure.storage import create_storage
from app.infrastructure.task_queue import create_task_queue
from app.infrastructure.vector_index import create_vector_index
from app.parsing import ParsingService
from app.services.ingestion import IngestionWorker
from app.services.outbox import OutboxDispatcher


async def run() -> None:
    settings = get_settings()
    settings.ensure_directories()
    database = create_database(settings)
    await database.initialize()
    storage = create_storage(settings)
    await storage.initialize()
    queue = await create_task_queue(settings)
    vector_index = create_vector_index(settings)
    if vector_index:
        await vector_index.initialize()
    repository = Repository(database, vector_outbox=vector_index is not None)
    tracing = Tracing.from_settings(settings)
    embedding, chat = create_providers(settings)
    embedding.tracing = chat.tracing = tracing
    parser = ParsingService(settings.artifact_dir, settings.ocr_engine)
    worker = IngestionWorker(
        repository, parser, embedding, queue, settings, storage=storage, chat_provider=chat,
    )
    worker.tracing = tracing
    outbox = OutboxDispatcher(repository, vector_index)
    await worker.start()
    await outbox.start()
    try:
        await asyncio.Event().wait()
    finally:
        await outbox.stop()
        await worker.stop()
        if vector_index:
            await vector_index.close()
        await queue.close()
        await storage.close()
        await database.close()
        await tracing.close()


if __name__ == "__main__":
    asyncio.run(run())
