import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.infrastructure.tracing import current_observation, traced
from app.services.chunker import build_parent_chunks, split_document
from app.domain import ChunkDraft
from app.core.errors import AppError, ParseError
from app.parsing import ParsingService
from app.infrastructure.repository import Repository, now_iso
from app.services.retrieval import tokenize
from app.services.wiki import WikiGenerationService
from app.infrastructure.storage import LocalStorage, safe_filename


class IngestionService:
    def __init__(self, repo: Repository, storage: LocalStorage, queue, settings):
        self.repo, self.storage, self.queue, self.settings = repo, storage, queue, settings

    async def create_file(self, kb_id: str, upload) -> dict:
        kb = await self.repo.get_kb(kb_id)
        if not kb:
            raise AppError("knowledge_base_not_found", "知识库不存在", 404)
        knowledge_id, filename = str(uuid4()), safe_filename(upload.filename or "document")
        final_dir = self.storage.knowledge_dir(kb_id, knowledge_id)
        final_dir.mkdir(parents=True, exist_ok=True)
        final_path = final_dir / filename
        digest, size = hashlib.sha256(), 0
        try:
            with final_path.open("wb") as handle:
                while block := await upload.read(1024 * 1024):
                    size += len(block)
                    if size > self.settings.max_upload_mb * 1024 * 1024:
                        raise AppError("file_too_large", f"文件不能超过 {self.settings.max_upload_mb} MB", 413)
                    digest.update(block)
                    handle.write(block)
            if size == 0:
                raise AppError("empty_file", "上传文件不能为空", 422)
            data = {
                "id": knowledge_id, "knowledge_base_id": kb_id, "title": filename,
                "source_type": "file", "file_name": filename, "file_path": str(final_path),
                "file_hash": digest.hexdigest(),
            }
            try:
                knowledge, task_id = await self.repo.create_knowledge_and_task(data, self.settings.worker_max_attempts)
            except Exception as exc:
                if "UNIQUE constraint failed" in str(exc):
                    raise AppError("duplicate_document", "该文件已经存在", 409) from exc
                raise
            await self.storage.persist_source(kb_id, knowledge_id, final_path)
            await self.queue.put(task_id)
            return knowledge
        except Exception:
            if final_path.exists():
                final_path.unlink()
            raise

    async def create_manual(self, kb_id: str, title: str, content: str) -> dict:
        kb = await self.repo.get_kb(kb_id)
        if not kb:
            raise AppError("knowledge_base_not_found", "知识库不存在", 404)
        if not content.strip():
            raise AppError("empty_document", "知识内容不能为空")
        knowledge_id = str(uuid4())
        filename = safe_filename(title) + ("" if title.lower().endswith(".md") else ".md")
        directory = self.storage.knowledge_dir(kb_id, knowledge_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_text(content, encoding="utf-8")
        data = {
            "id": knowledge_id, "knowledge_base_id": kb_id, "title": title,
            "source_type": "manual", "file_name": filename, "file_path": str(path),
            "file_hash": None,
        }
        knowledge, task_id = await self.repo.create_knowledge_and_task(data, self.settings.worker_max_attempts)
        await self.storage.persist_source(kb_id, knowledge_id, path)
        await self.queue.put(task_id)
        return knowledge

    async def reprocess(self, knowledge_id: str) -> dict:
        knowledge = await self.repo.get_knowledge(knowledge_id)
        if not knowledge:
            raise AppError("knowledge_not_found", "知识不存在", 404)
        active = await self.repo.query_one(
            "SELECT id FROM ingestion_tasks WHERE knowledge_id=? AND status IN ('queued','running')", (knowledge_id,)
        )
        if active:
            raise AppError("processing_in_progress", "该知识正在处理中", 409)
        task_id = str(uuid4())
        stamp = now_iso()
        await self.repo.execute(
            "INSERT INTO ingestion_tasks(id,knowledge_id,status,attempts,max_attempts,available_at,created_at,updated_at) VALUES (?,?, 'queued',0,?,?,?,?)",
            (task_id, knowledge_id, self.settings.worker_max_attempts, stamp, stamp, stamp),
        )
        await self.repo.execute(
            "UPDATE knowledges SET parse_status='pending',current_stage='queued',error_message='',updated_at=? WHERE id=?",
            (stamp, knowledge_id),
        )
        await self.queue.put(task_id)
        return await self.repo.get_knowledge(knowledge_id)


class IngestionWorker:
    def __init__(self, repo, parser: ParsingService, embedding, queue, settings,
                 storage=None, wiki=None, chat_provider=None):
        self.repo, self.parser, self.embedding = repo, parser, embedding
        self.queue, self.settings = queue, settings
        self.storage = storage
        self.wiki = wiki or WikiGenerationService(
            chat_provider, settings.provider_mode, settings.wiki_extraction_granularity, settings.wiki_max_candidates,
        )
        self.runner: asyncio.Task | None = None
        self.stopping = False

    async def start(self) -> None:
        for task_id in await self.repo.recover_tasks(self.settings.task_lease_seconds):
            await self.queue.put(task_id)
        self.runner = asyncio.create_task(self.run(), name="ingestion-worker")

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
            task_id = await self.queue.get()
            try:
                await self.process(task_id)
            finally:
                await self.queue.task_done()

    @traced("document-ingestion")
    async def process(self, task_id: str) -> None:
        task = await self.repo.claim_task(task_id)
        if not task:
            return
        knowledge = await self.repo.get_knowledge(task["knowledge_id"])
        if not knowledge:
            return
        current_observation().update(input={"task_id": task_id, "knowledge_id": knowledge["id"], "attempt": task["attempts"]})
        await self.repo.mark_processing(knowledge["id"])
        current_stage = None
        try:
            current_stage = await self.repo.start_stage(knowledge["id"], "parsing", {"file": knowledge["file_name"]})
            source_path = Path(knowledge["file_path"])
            if self.storage:
                source_path = await self.storage.ensure_source(
                    knowledge["knowledge_base_id"], knowledge["id"], knowledge["file_name"],
                )
            parsed = await self.parser.parse(source_path, knowledge["id"], knowledge["title"])
            if self.storage:
                await self.storage.persist_artifacts(knowledge["id"])
            await self.repo.finish_stage(current_stage, parsed.manifest()["quality"])

            current_stage = await self.repo.start_stage(knowledge["id"], "chunking", {"characters": len(parsed.markdown)})
            kb = await self.repo.get_kb(knowledge["knowledge_base_id"])
            drafts = split_document(
                parsed.markdown, kb["chunk_size"], kb["chunk_overlap"], kb["parent_chunk_size"],
            )
            if not drafts:
                raise ParseError("empty_chunks", "文档没有生成有效分块")
            await self.repo.finish_stage(current_stage, {"chunks": len(drafts)})

            current_stage = await self.repo.start_stage(knowledge["id"], "embedding", {"chunks": len(drafts)})
            texts = [self._embedding_text(knowledge["title"], x.heading_path, x.content) for x in drafts]
            vectors = []
            provider_limit = getattr(self.embedding, "max_batch_size", None)
            batch_size = min(self.settings.embedding_batch_size, provider_limit) if provider_limit else self.settings.embedding_batch_size
            for offset in range(0, len(texts), batch_size):
                vectors.extend(await self.embedding.embed(texts[offset:offset + batch_size]))
            if len(vectors) != len(drafts) or any(len(x) != kb["embedding_dimension"] for x in vectors):
                raise AppError("embedding_dimension_mismatch", "Embedding 数量或维度与知识库配置不一致", 409)
            await self.repo.finish_stage(current_stage, {"vectors": len(vectors), "dimension": len(vectors[0])})

            current_stage = await self.repo.start_stage(knowledge["id"], "indexing", {"chunks": len(drafts)})
            parent_rows, parent_ids = [], {}
            for parent in build_parent_chunks(drafts):
                parent_id = str(uuid4())
                parent_ids[parent.parent_index] = parent_id
                parent_rows.append({
                    "id": parent_id, "parent_index": parent.parent_index, "content": parent.content,
                    "heading_path": parent.heading_path, "page_number": parent.page_number,
                    "content_hash": hashlib.sha256(parent.content.encode()).hexdigest(),
                })
            rows = []
            for draft, vector in zip(drafts, vectors):
                rows.append({
                    "id": str(uuid4()), "chunk_index": draft.chunk_index, "heading_path": draft.heading_path,
                    "page_number": draft.page_number, "content": draft.content,
                    "parent_id": parent_ids.get(draft.parent_index),
                    "embedding": vector, "content_hash": hashlib.sha256(draft.content.encode()).hexdigest(),
                    "title_tokens": " ".join(tokenize(knowledge["title"])),
                    "heading_tokens": " ".join(tokenize(draft.heading_path)),
                    "content_tokens": " ".join(tokenize(draft.content)),
                })
            await self.repo.replace_index(knowledge, parsed.parser_engine, parent_rows, rows)
            await self.repo.finish_stage(current_stage, {"indexed": len(rows)})

            current_stage = await self.repo.start_stage(knowledge["id"], "wiki_generation", {"chunks": len(drafts)})
            indexed_chunks = await self.repo.list_chunks(knowledge["id"])
            wiki_chunks = [ChunkDraft(
                chunk_index=row["chunk_index"], content=row["content"], heading_path=row["heading_path"],
                page_number=row["page_number"], parent_content=row["parent_content"], id=row["id"],
                parent_index=row.get("parent_index"),
            ) for row in indexed_chunks]
            existing_pages = await self.repo.list_wiki_pages_with_contributions(knowledge["knowledge_base_id"])
            build_id = await self.repo.start_wiki_build(knowledge)
            try:
                wiki_pages, wiki_meta = await self.wiki.generate(
                    knowledge, parsed, wiki_chunks, existing_pages, repo=self.repo, build_id=build_id,
                )
            except Exception as exc:
                await self.repo.fail_wiki_build(build_id, "pipeline", str(exc))
                raise
            await self.repo.start_wiki_build_stage(build_id, "persist", {"pages": len(wiki_pages)})
            await self.repo.replace_wiki_pages(knowledge, wiki_pages)
            await self.repo.apply_wiki_taxonomy(knowledge["knowledge_base_id"], wiki_meta.pop("taxonomy"))
            await self.repo.finish_wiki_build_stage(build_id, "persist", {"pages": len(wiki_pages)})
            await self.repo.start_wiki_build_stage(build_id, "finalize", {"knowledge_base_id": knowledge["knowledge_base_id"]})
            await self.repo.finalize_wiki(knowledge["knowledge_base_id"])
            await self.repo.finish_wiki_build_stage(build_id, "finalize", await self.repo.wiki_stats(knowledge["knowledge_base_id"]))
            await self.repo.start_wiki_build_stage(build_id, "quality", {"checks": ["dead_link", "missing_source"]})
            issues = await self.repo.persist_wiki_quality(knowledge["knowledge_base_id"], build_id)
            await self.repo.finish_wiki_build_stage(build_id, "quality", {"issues": len(issues)})
            wiki_meta["quality_issues"] = len(issues)
            await self.repo.finish_wiki_build(build_id, wiki_meta["mode"], wiki_meta)
            await self.repo.finish_stage(current_stage, {"pages": len(wiki_pages), **wiki_meta})
            await self.repo.mark_completed(knowledge["id"])
            await self.repo.task_succeeded(task_id)
            current_observation().update(output={"status": "succeeded", "chunks": len(drafts), "pages": len(wiki_pages)})
        except Exception as exc:
            current_observation().error(exc)
            code = getattr(exc, "code", "processing_failed")
            if current_stage:
                await self.repo.finish_stage(current_stage, error=(code, str(exc)))
            retryable = getattr(exc, "retryable", False) or not isinstance(exc, (AppError, ValueError))
            retry = await self.repo.task_failed(task, str(exc), retryable)
            if retry:
                await asyncio.sleep(min(30, 2 ** task["attempts"]))
                await self.queue.put(task_id)

    @staticmethod
    def _embedding_text(title: str, heading: str, content: str) -> str:
        parts = [f"文档：{title}"]
        if heading:
            parts.append(f"章节：{heading}")
        parts.append(f"内容：{content}")
        return "\n".join(parts)
