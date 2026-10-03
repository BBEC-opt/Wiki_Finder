"""把 PostgreSQL/SQLite 中已提交的索引事件投影到 Qdrant。"""

import asyncio
import json


class OutboxDispatcher:
    def __init__(self, repository, vector_index, poll_seconds: float = 0.5):
        self.repo = repository
        self.vector_index = vector_index
        self.poll_seconds = poll_seconds
        self.runner: asyncio.Task | None = None
        self.stopping = False

    async def start(self) -> None:
        if self.vector_index is not None:
            await self.repo.recover_outbox()
            self.runner = asyncio.create_task(self.run(), name="vector-outbox")

    async def stop(self) -> None:
        self.stopping = True
        if self.runner:
            self.runner.cancel()
            try:
                await self.runner
            except asyncio.CancelledError:
                pass

    async def run(self) -> None:
        while not self.stopping:
            events = await self.repo.claim_outbox(20)
            if not events:
                await asyncio.sleep(self.poll_seconds)
                continue
            for event in events:
                try:
                    payload = json.loads(event["payload"])
                    if event["event_type"] == "vector.upsert":
                        await self.vector_index.upsert(payload["dimension"], payload["points"])
                    elif event["event_type"] == "vector.delete_knowledge":
                        await self.vector_index.delete_knowledge(payload["dimension"], payload["knowledge_id"])
                    await self.repo.complete_outbox(event["id"])
                except Exception as exc:
                    await self.repo.fail_outbox(event["id"], str(exc))
