"""任务通知通道；任务所有权和最终状态始终保存在数据库。"""

from __future__ import annotations

import asyncio
from uuid import uuid4


class MemoryTaskQueue:
    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    async def initialize(self) -> None:
        pass

    async def put(self, task_id: str) -> None:
        await self.queue.put(task_id)

    async def get(self) -> str:
        return await self.queue.get()

    async def task_done(self) -> None:
        self.queue.task_done()

    async def close(self) -> None:
        pass


class RedisTaskQueue:
    def __init__(self, url: str, prefix: str, lease_seconds: int):
        self.url = url
        self.stream = f"{prefix}:ingestion"
        self.group = f"{prefix}:workers"
        self.consumer = str(uuid4())
        self.client = None
        self.pending_message_id: str | None = None
        self.lease_milliseconds = lease_seconds * 1_000

    async def initialize(self) -> None:
        try:
            from redis.asyncio import from_url
            from redis.exceptions import ResponseError
        except ImportError as exc:
            raise RuntimeError('Redis 模式需要安装项目的 "production" 可选依赖') from exc
        self.client = from_url(self.url, decode_responses=True, health_check_interval=30)
        await self.client.ping()
        try:
            await self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def put(self, task_id: str) -> None:
        await self.client.xadd(self.stream, {"task_id": task_id, "version": "1"}, maxlen=100_000)

    async def get(self) -> str:
        while True:
            claimed = await self.client.xautoclaim(
                self.stream, self.group, self.consumer, self.lease_milliseconds, "0-0", count=1,
            )
            entries = claimed[1] if len(claimed) > 1 else []
            if entries:
                message_id, fields = entries[0]
                self.pending_message_id = message_id
                return fields["task_id"]
            messages = await self.client.xreadgroup(
                self.group, self.consumer, {self.stream: ">"}, count=1, block=1_000,
            )
            if not messages:
                continue
            _, entries = messages[0]
            message_id, fields = entries[0]
            self.pending_message_id = message_id
            return fields["task_id"]

    async def task_done(self) -> None:
        if self.pending_message_id:
            message_id = self.pending_message_id
            self.pending_message_id = None
            await self.client.xack(self.stream, self.group, message_id)

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()


async def create_task_queue(settings):
    if settings.task_notifier_driver == "redis":
        queue = RedisTaskQueue(settings.redis_url, settings.redis_prefix, settings.task_lease_seconds)
    else:
        queue = MemoryTaskQueue()
    await queue.initialize()
    return queue
