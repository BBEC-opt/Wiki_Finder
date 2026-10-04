"""可选 Langfuse 追踪；显式父子关系避免 SSE yield 泄漏活动上下文。"""

import asyncio
import inspect
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

from app.core.config import Settings

logger = logging.getLogger(__name__)
_current: ContextVar["Observation | None"] = ContextVar("wiki_observation", default=None)


class Observation:
    def __init__(self, tracing: "Tracing", span: Any = None, session_id: str | None = None):
        self.tracing, self.span, self.session_id = tracing, span, session_id

    def update(self, **values: Any) -> None:
        if self.span is not None:
            try:
                self.span.update(**values)
            except Exception:
                logger.warning("Langfuse 更新失败")

    def content(self, **values: Any) -> None:
        if self.tracing.capture_content:
            self.update(**values)

    def error(self, exc: BaseException) -> None:
        # 异常正文可能含模型响应或凭据，只记录类型。
        self.update(level="ERROR", status_message=type(exc).__name__)

    def end(self) -> None:
        if self.span is not None:
            try:
                self.span.end()
            except Exception:
                logger.warning("Langfuse 结束追踪失败")


class Tracing:
    def __init__(self, client: Any = None, capture_content: bool = False):
        self.client, self.capture_content = client, capture_content

    @classmethod
    def from_settings(cls, settings: Settings) -> "Tracing":
        if not settings.langfuse_enabled:
            return cls()
        try:
            from langfuse import Langfuse
        except ImportError as exc:
            raise RuntimeError('启用 Langfuse 需要安装可选依赖：pip install -e ".[langfuse]"') from exc
        try:
            client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                base_url=settings.langfuse_base_url,
                environment=settings.langfuse_tracing_environment,
                tracing_enabled=True,
            )
        except Exception:
            logger.warning("Langfuse 初始化失败，已关闭追踪")
            return cls()
        return cls(client, settings.langfuse_capture_content)

    def start(self, name: str, as_type: str, session_id: str | None = None) -> Observation:
        parent = _current.get()
        if parent is not None and parent.tracing is not self:
            parent = None
        session_id = session_id or (parent.session_id if parent else None)
        span = None
        if self.client is not None:
            try:
                from langfuse import propagate_attributes
                with propagate_attributes(session_id=session_id, tags=["my-wiki"]):
                    owner = parent.span if parent and parent.span is not None else self.client
                    span = owner.start_observation(name=name, as_type=as_type)
            except Exception:
                logger.warning("Langfuse 创建追踪失败")
        return Observation(self, span, session_id)

    async def close(self) -> None:
        if self.client is not None:
            try:
                await asyncio.to_thread(self.client.shutdown)
            except Exception:
                logger.warning("Langfuse 关闭失败")


_disabled = Tracing()


def current_observation() -> Observation:
    return _current.get() or Observation(_disabled)


@contextmanager
def _activate(observation: Observation) -> Iterator[None]:
    token = _current.set(observation)
    try:
        yield
    finally:
        _current.reset(token)


def traced(
    name: str, as_type: str = "span", *, session: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """只捕获显式写入的数据；生成器每次推进后恢复上下文。"""
    def decorate(function):
        def start(instance, args, kwargs):
            parent = _current.get()
            tracing = getattr(instance, "tracing", None) or (parent.tracing if parent else _disabled)
            request = (args[0] if args else kwargs.get("request")) if session else None
            return tracing.start(name, as_type, getattr(request, "session_id", None))

        if inspect.isasyncgenfunction(function):
            @wraps(function)
            async def stream(instance, *args, **kwargs):
                observation = start(instance, args, kwargs)
                iterator = function(instance, *args, **kwargs)
                try:
                    while True:
                        with _activate(observation):
                            try:
                                item = await anext(iterator)
                            except StopAsyncIteration:
                                break
                        yield item
                except BaseException as exc:
                    observation.error(exc)
                    raise
                finally:
                    try:
                        with _activate(observation):
                            await iterator.aclose()
                    finally:
                        observation.end()
            return stream

        @wraps(function)
        async def call(instance, *args, **kwargs):
            observation = start(instance, args, kwargs)
            try:
                with _activate(observation):
                    return await function(instance, *args, **kwargs)
            except BaseException as exc:
                observation.error(exc)
                raise
            finally:
                observation.end()
        return call
    return decorate
