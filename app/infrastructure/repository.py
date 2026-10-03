import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.infrastructure.database import Database


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, database: Database, vector_outbox: bool = False):
        self.database = database
        self.vector_outbox = vector_outbox

    async def create_kb(self, data: dict[str, Any]) -> dict[str, Any]:
        stamp, kb_id = now_iso(), str(uuid4())
        db = await self.database.connect()
        try:
            await db.execute(
                """INSERT INTO knowledge_bases
                (id,name,description,chunk_size,chunk_overlap,parent_chunk_size,embedding_provider,embedding_model,embedding_dimension,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (kb_id, data["name"], data.get("description", ""), data["chunk_size"], data["chunk_overlap"],
                 data["parent_chunk_size"], data["embedding_provider"], data["embedding_model"],
                 data["embedding_dimension"], stamp, stamp),
            )
            await db.commit()
            return await self._one(db, "SELECT * FROM knowledge_bases WHERE id=?", (kb_id,))
        finally:
            await db.close()

    async def list_kbs(self) -> list[dict]:
        return await self.query_all("SELECT * FROM knowledge_bases ORDER BY created_at DESC")

    async def get_kb(self, kb_id: str) -> dict | None:
        return await self.query_one("SELECT * FROM knowledge_bases WHERE id=?", (kb_id,))

    async def create_knowledge_and_task(self, knowledge: dict, max_attempts: int) -> tuple[dict, str]:
        stamp, task_id = now_iso(), str(uuid4())
        db = await self.database.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """INSERT INTO knowledges
                (id,knowledge_base_id,title,source_type,file_name,file_path,file_hash,parse_status,current_stage,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (knowledge["id"], knowledge["knowledge_base_id"], knowledge["title"], knowledge["source_type"],
                 knowledge.get("file_name"), knowledge.get("file_path"), knowledge.get("file_hash"),
                 "pending", "queued", stamp, stamp),
            )
            await db.execute(
                """INSERT INTO ingestion_tasks
                (id,knowledge_id,status,attempts,max_attempts,available_at,created_at,updated_at)
                VALUES (?,?, 'queued',0,?,?,?,?)""",
                (task_id, knowledge["id"], max_attempts, stamp, stamp, stamp),
            )
            await db.commit()
            return await self._one(db, "SELECT * FROM knowledges WHERE id=?", (knowledge["id"],)), task_id
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_knowledge(self, knowledge_id: str) -> dict | None:
        return await self.query_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))

    async def list_knowledge(self, kb_id: str) -> list[dict]:
        return await self.query_all("SELECT * FROM knowledges WHERE knowledge_base_id=? ORDER BY created_at DESC", (kb_id,))

    async def list_chunks(self, knowledge_id: str) -> list[dict]:
        rows = await self.query_all(
            """SELECT c.*,p.content explicit_parent_content,p.parent_index FROM chunks c
            LEFT JOIN parent_chunks p ON p.id=c.parent_id WHERE c.knowledge_id=? ORDER BY c.chunk_index""",
            (knowledge_id,),
        )
        for row in rows:
            row.pop("embedding", None)
            row["parent_content"] = row.pop("explicit_parent_content") or row.get("parent_content")
        return rows

    async def list_stages(self, knowledge_id: str) -> list[dict]:
        return await self.query_all("SELECT * FROM processing_stages WHERE knowledge_id=? ORDER BY id", (knowledge_id,))

    async def list_wiki_pages(self, kb_id: str, knowledge_id: str | None = None, query: str = "") -> list[dict]:
        sql = "SELECT * FROM wiki_pages WHERE knowledge_base_id=?"
        params: list = [kb_id]
        if knowledge_id:
            sql += " AND (knowledge_id=? OR id IN (SELECT page_id FROM wiki_page_sources WHERE knowledge_id=?))"
            params.extend([knowledge_id, knowledge_id])
        if query:
            sql += " AND (title LIKE ? OR summary LIKE ? OR content LIKE ?)"
            pattern = f"%{query}%"
            params.extend([pattern, pattern, pattern])
        rows = await self.query_all(sql + " ORDER BY position, title", tuple(params))
        for row in rows:
            row["source_refs"] = json.loads(row["source_refs"])
        return rows

    async def list_wiki_pages_with_contributions(self, kb_id: str) -> list[dict]:
        pages = await self.list_wiki_pages(kb_id)
        folders = {folder["id"]: folder for folder in await self.list_wiki_folders(kb_id)}
        def folder_path(folder_id: str | None) -> str:
            names, seen = [], set()
            while folder_id and folder_id in folders and folder_id not in seen:
                seen.add(folder_id)
                folder = folders[folder_id]
                names.append(folder["name"])
                folder_id = folder["parent_id"]
            return "/".join(reversed(names))
        for page in pages:
            page["folder_path"] = folder_path(page.get("folder_id"))
            contributions = await self.query_all(
                """SELECT c.*,k.title knowledge_title FROM wiki_page_contributions c
                JOIN knowledges k ON k.id=c.knowledge_id WHERE c.page_id=? ORDER BY k.created_at""", (page["id"],)
            )
            for contribution in contributions:
                contribution["chunk_ids"] = json.loads(contribution.pop("chunk_ids_json"))
            page["contributions"] = contributions
        return pages

    async def start_wiki_build(self, knowledge: dict) -> str:
        build_id, stamp = str(uuid4()), now_iso()
        await self.execute(
            "INSERT INTO wiki_builds(id,knowledge_base_id,knowledge_id,status,started_at) VALUES (?,?,?,'running',?)",
            (build_id, knowledge["knowledge_base_id"], knowledge["id"], stamp),
        )
        return build_id

    async def start_wiki_build_stage(self, build_id: str, stage: str, inputs: dict) -> None:
        await self.execute(
            """INSERT INTO wiki_build_stages(build_id,stage,status,started_at,input_json)
            VALUES (?,?,'running',?,?) ON CONFLICT(build_id,stage) DO UPDATE SET
            status='running',started_at=excluded.started_at,finished_at=NULL,input_json=excluded.input_json,
            output_json='{}',error_message=''""",
            (build_id, stage, now_iso(), json.dumps(inputs, ensure_ascii=False)),
        )

    async def finish_wiki_build_stage(self, build_id: str, stage: str, output: dict) -> None:
        await self.execute(
            "UPDATE wiki_build_stages SET status='completed',finished_at=?,output_json=? WHERE build_id=? AND stage=?",
            (now_iso(), json.dumps(output, ensure_ascii=False), build_id, stage),
        )

    async def fail_wiki_build(self, build_id: str, stage: str, error: str) -> None:
        stamp = now_iso()
        if stage == "pipeline":
            await self.execute(
                "UPDATE wiki_build_stages SET status='failed',finished_at=?,error_message=? WHERE build_id=? AND status='running'",
                (stamp, error[:2000], build_id),
            )
        else:
            await self.execute(
                "UPDATE wiki_build_stages SET status='failed',finished_at=?,error_message=? WHERE build_id=? AND stage=?",
                (stamp, error[:2000], build_id, stage),
            )
        await self.execute(
            "UPDATE wiki_builds SET status='failed',finished_at=?,error_message=? WHERE id=?",
            (stamp, error[:2000], build_id),
        )

    async def finish_wiki_build(self, build_id: str, mode: str, stats: dict) -> None:
        status = "degraded" if mode != "model" or stats.get("candidates", 0) == 0 else "completed"
        await self.execute(
            "UPDATE wiki_builds SET status=?,mode=?,finished_at=?,stats_json=? WHERE id=?",
            (status, mode, now_iso(), json.dumps(stats, ensure_ascii=False), build_id),
        )

    async def wiki_build_details(self, build_id: str) -> dict | None:
        build = await self.query_one("SELECT * FROM wiki_builds WHERE id=?", (build_id,))
        if not build:
            return None
        build["stats"] = json.loads(build.pop("stats_json"))
        stages = await self.query_all("SELECT * FROM wiki_build_stages WHERE build_id=? ORDER BY id", (build_id,))
        for stage in stages:
            stage["input"] = json.loads(stage.pop("input_json"))
            stage["output"] = json.loads(stage.pop("output_json"))
        build["stages"] = stages
        return build

    async def list_wiki_builds(self, knowledge_id: str) -> list[dict]:
        rows = await self.query_all(
            "SELECT * FROM wiki_builds WHERE knowledge_id=? ORDER BY started_at DESC", (knowledge_id,)
        )
        for row in rows:
            row["stats"] = json.loads(row.pop("stats_json"))
        return rows

    async def get_wiki_page(self, page_id: str) -> dict | None:
        row = await self.query_one("SELECT * FROM wiki_pages WHERE id=?", (page_id,))
        if row:
            row["source_refs"] = json.loads(row["source_refs"])
        return row

    async def get_wiki_page_by_slug(self, kb_id: str, slug: str) -> dict | None:
        row = await self.query_one("SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug=?", (kb_id, slug))
        if row:
            row["source_refs"] = json.loads(row["source_refs"])
        return row

    async def wiki_page_sources(self, page_id: str) -> list[dict]:
        return await self.query_all(
            """SELECT s.*, k.title knowledge_title, c.heading_path, c.content chunk_content
            FROM wiki_page_sources s JOIN knowledges k ON k.id=s.knowledge_id
            LEFT JOIN chunks c ON c.id=s.chunk_id WHERE s.page_id=? ORDER BY k.title, c.chunk_index""",
            (page_id,),
        )

    async def wiki_page_backlinks(self, page_id: str) -> list[dict]:
        return await self.query_all(
            """SELECT p.id,p.slug,p.title,p.summary FROM wiki_page_links l
            JOIN wiki_pages p ON p.id=l.source_page_id WHERE l.target_page_id=? ORDER BY p.title""",
            (page_id,),
        )

    async def rebuild_wiki_links(self, page_id: str, kb_id: str, content: str) -> None:
        slugs = set(re.findall(r"\[\[([a-z0-9][a-z0-9/_-]*)(?:\|[^\]]+)?\]\]", content, re.IGNORECASE))
        db = await self.database.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DELETE FROM wiki_page_links WHERE source_page_id=?", (page_id,))
            for slug in slugs:
                target = await self._one(db, "SELECT id FROM wiki_pages WHERE knowledge_base_id=? AND slug=?", (kb_id, slug))
                await db.execute(
                    "INSERT INTO wiki_page_links(source_page_id,target_page_id,target_slug) VALUES (?,?,?)",
                    (page_id, target["id"] if target else None, slug),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def wiki_slug_for_chunk(self, kb_id: str, chunk_id: str) -> str | None:
        row = await self.query_one(
            """SELECT p.slug FROM wiki_page_sources s JOIN wiki_pages p ON p.id=s.page_id
            WHERE p.knowledge_base_id=? AND s.chunk_id=? ORDER BY p.page_type='source', p.position LIMIT 1""",
            (kb_id, chunk_id),
        )
        return row["slug"] if row else None

    async def save_wiki_page(self, kb_id: str, data: dict, page_id: str | None = None) -> dict:
        stamp = now_iso()
        if page_id:
            await self.execute(
                """UPDATE wiki_pages SET slug=?,title=?,summary=?,content=?,page_type=?,status=?,folder_id=?,
                edit_source='user',version=version+1,updated_at=? WHERE id=? AND knowledge_base_id=?""",
                (data["slug"], data["title"], data.get("summary", ""), data["content"], data["page_type"],
                 data["status"], data.get("folder_id"), stamp, page_id, kb_id),
            )
        else:
            page_id = str(uuid4())
            await self.execute(
                """INSERT INTO wiki_pages
                (id,knowledge_base_id,knowledge_id,slug,title,summary,content,page_type,status,folder_id,
                 edit_source,version,position,source_refs,created_at,updated_at)
                VALUES (?,?,NULL,?,?,?,?,?,?,?,'user',1,0,'[]',?,?)""",
                (page_id, kb_id, data["slug"], data["title"], data.get("summary", ""), data["content"],
                 data["page_type"], data["status"], data.get("folder_id"), stamp, stamp),
            )
        await self.rebuild_wiki_links(page_id, kb_id, data["content"])
        return await self.get_wiki_page(page_id)

    async def list_wiki_folders(self, kb_id: str) -> list[dict]:
        return await self.query_all(
            "SELECT * FROM wiki_folders WHERE knowledge_base_id=? ORDER BY position,name", (kb_id,)
        )

    async def create_wiki_folder(self, kb_id: str, name: str, parent_id: str | None) -> dict:
        folder_id, stamp = str(uuid4()), now_iso()
        await self.execute(
            "INSERT INTO wiki_folders(id,knowledge_base_id,parent_id,name,position,created_at,updated_at) VALUES (?,?,?,?,0,?,?)",
            (folder_id, kb_id, parent_id, name, stamp, stamp),
        )
        return await self.query_one("SELECT * FROM wiki_folders WHERE id=?", (folder_id,))

    async def replace_wiki_pages(self, knowledge: dict, pages: list[dict]) -> None:
        stamp = now_iso()
        db = await self.database.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            old_sources = await self._all(
                db, """SELECT DISTINCT page_id FROM wiki_page_sources WHERE knowledge_id=?
                UNION SELECT id page_id FROM wiki_pages WHERE knowledge_id=? AND edit_source='pipeline'""",
                (knowledge["id"], knowledge["id"]),
            )
            await db.execute("DELETE FROM wiki_page_sources WHERE knowledge_id=?", (knowledge["id"],))
            await db.execute("DELETE FROM wiki_page_contributions WHERE knowledge_id=?", (knowledge["id"],))
            refreshed_page_ids = set()
            for page in pages:
                folder_name = str(page.get("folder", "")).strip()
                folder_id = None
                if folder_name:
                    parent_id = None
                    for folder_part in [part.strip() for part in folder_name.split("/")[:2] if part.strip()]:
                        if parent_id:
                            folder = await self._one(db, "SELECT id FROM wiki_folders WHERE knowledge_base_id=? AND parent_id=? AND name=?", (knowledge["knowledge_base_id"], parent_id, folder_part))
                        else:
                            folder = await self._one(db, "SELECT id FROM wiki_folders WHERE knowledge_base_id=? AND parent_id IS NULL AND name=?", (knowledge["knowledge_base_id"], folder_part))
                        if folder:
                            folder_id = folder["id"]
                        else:
                            folder_id = str(uuid4())
                            await db.execute(
                                "INSERT INTO wiki_folders(id,knowledge_base_id,parent_id,name,position,created_at,updated_at) VALUES (?,?,?,?,0,?,?)",
                                (folder_id, knowledge["knowledge_base_id"], parent_id, folder_part, stamp, stamp),
                            )
                        parent_id = folder_id
                existing = await self._one(db, "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug=?",
                                           (knowledge["knowledge_base_id"], page["slug"]))
                if existing and existing["edit_source"] == "user":
                    page_id = existing["id"]
                elif existing:
                    page_id = existing["id"]
                    await db.execute(
                        "UPDATE wiki_pages SET title=?,summary=?,content=?,folder_id=?,updated_at=?,version=version+1 WHERE id=?",
                        (page["title"], page["summary"], page["content"], folder_id, stamp, page_id),
                    )
                else:
                    page_id = page["id"]
                    await db.execute(
                        """INSERT INTO wiki_pages
                        (id,knowledge_base_id,knowledge_id,slug,title,summary,content,page_type,status,folder_id,edit_source,
                         version,position,source_refs,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?, 'published',?,'pipeline',1,?,?,?,?)""",
                        (page_id, knowledge["knowledge_base_id"], knowledge["id"], page["slug"], page["title"],
                         page["summary"], page["content"], page["page_type"], folder_id, page["position"],
                         json.dumps(page["source_refs"], ensure_ascii=False), stamp, stamp),
                    )
                chunk_ids = sorted({chunk_id for ref in page.get("source_refs", [])
                                    if ref.get("knowledge_id") == knowledge["id"]
                                    for chunk_id in ref.get("chunk_ids", [])})
                indices = sorted({index for ref in page.get("source_refs", [])
                                  if ref.get("knowledge_id") == knowledge["id"]
                                  for index in ref.get("chunk_indices", [])})
                if chunk_ids:
                    marks = ",".join("?" for _ in chunk_ids)
                    cited = await self._all(
                        db, f"SELECT id,page_number,content FROM chunks WHERE knowledge_id=? AND id IN ({marks}) ORDER BY chunk_index",
                        (knowledge["id"], *chunk_ids),
                    )
                elif indices:
                    marks = ",".join("?" for _ in indices)
                    cited = await self._all(
                        db, f"SELECT id,page_number,content FROM chunks WHERE knowledge_id=? AND chunk_index IN ({marks}) ORDER BY chunk_index",
                        (knowledge["id"], *indices),
                    )
                else:
                    cited = []
                if not cited:
                    cited = [{"id": None, "page_number": None, "content": page["summary"]}]
                for chunk in cited:
                    await db.execute(
                        "INSERT OR REPLACE INTO wiki_page_sources(page_id,knowledge_id,chunk_id,page_number,excerpt) VALUES (?,?,?,?,?)",
                        (page_id, knowledge["id"], chunk["id"], chunk["page_number"], chunk["content"][:500]),
                    )
                contribution = page.get("contribution")
                if contribution:
                    await db.execute(
                        """INSERT INTO wiki_page_contributions
                        (page_id,knowledge_id,summary,content,chunk_ids_json,updated_at) VALUES (?,?,?,?,?,?)
                        ON CONFLICT(page_id,knowledge_id) DO UPDATE SET summary=excluded.summary,content=excluded.content,
                        chunk_ids_json=excluded.chunk_ids_json,updated_at=excluded.updated_at""",
                        (page_id, knowledge["id"], contribution["summary"], contribution["content"],
                         json.dumps(contribution["chunk_ids"], ensure_ascii=False), stamp),
                    )
                refreshed_page_ids.add(page_id)
            stale_page_ids = [old["page_id"] for old in old_sources if old["page_id"] not in refreshed_page_ids]
            await self._refresh_pipeline_pages(db, stale_page_ids, stamp)
            for old in old_sources:
                await db.execute(
                    "DELETE FROM wiki_pages WHERE id=? AND edit_source='pipeline' AND NOT EXISTS (SELECT 1 FROM wiki_page_sources WHERE page_id=wiki_pages.id)",
                    (old["page_id"],),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def _refresh_pipeline_pages(self, db, page_ids: list[str], stamp: str) -> None:
        """来源减少后按剩余 contribution 重建自动页，防止正文残留已删除证据。"""
        for page_id in set(page_ids):
            page = await self._one(db, "SELECT * FROM wiki_pages WHERE id=?", (page_id,))
            if not page or page["edit_source"] != "pipeline" or page["page_type"] not in {"entity", "concept", "topic"}:
                continue
            contributions = await self._all(
                db,
                """SELECT c.*,k.title knowledge_title FROM wiki_page_contributions c
                JOIN knowledges k ON k.id=c.knowledge_id WHERE c.page_id=? ORDER BY k.created_at,c.knowledge_id""",
                (page_id,),
            )
            if not contributions:
                continue
            summaries = " ".join(item["summary"].strip() for item in contributions if item["summary"].strip())
            summary = summaries[:997] + "..." if len(summaries) > 1000 else summaries
            parts = [f"# {page['title']}"]
            for item in contributions:
                parts.extend([
                    "", f"<!-- source:{item['knowledge_id']} -->",
                    f"## 来源：{item['knowledge_title']}", "", item["content"].strip(),
                    f"<!-- /source:{item['knowledge_id']} -->",
                ])
            await db.execute(
                "UPDATE wiki_pages SET summary=?,content=?,version=version+1,updated_at=? WHERE id=?",
                (summary or page["summary"], "\n".join(parts), stamp, page_id),
            )

    async def finalize_wiki(self, kb_id: str) -> None:
        """在单一事务中重建索引、链接和空目录。"""
        db = await self.database.connect()
        stamp = now_iso()
        try:
            await db.execute("BEGIN IMMEDIATE")
            pages = await self._all(db, "SELECT * FROM wiki_pages WHERE knowledge_base_id=? ORDER BY position,title", (kb_id,))
            groups: dict[str, list[dict]] = {}
            for page in pages:
                if page["page_type"] not in {"index", "source"} and page["status"] != "archived":
                    groups.setdefault(page["page_type"], []).append(page)
            labels = {"summary": "文档概览", "entity": "实体", "concept": "概念", "topic": "主题"}
            lines = ["# Wiki 索引", "", "本页由系统根据当前知识库页面自动整理。"]
            for page_type in ("summary", "entity", "concept", "topic"):
                items = groups.get(page_type, [])
                if items:
                    lines.extend(["", f"## {labels[page_type]}", ""])
                    lines.extend(f"- [[{page['slug']}|{page['title']}]] — {page['summary']}" for page in items)
            content = "\n".join(lines)
            index = await self._one(db, "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug='index/home'", (kb_id,))
            if not index:
                await db.execute("""INSERT INTO wiki_pages
                    (id,knowledge_base_id,knowledge_id,slug,title,summary,content,page_type,status,edit_source,version,position,source_refs,created_at,updated_at)
                    VALUES (?,?,NULL,'index/home','Wiki 索引','知识库页面导航',?,'index','published','pipeline',1,-1,'[]',?,?)""",
                    (str(uuid4()), kb_id, content, stamp, stamp))
            elif index["edit_source"] == "pipeline":
                await db.execute("UPDATE wiki_pages SET content=?,version=version+1,updated_at=? WHERE id=?", (content, stamp, index["id"]))
            pages = await self._all(db, "SELECT id,slug,content FROM wiki_pages WHERE knowledge_base_id=?", (kb_id,))
            slug_ids = {page["slug"]: page["id"] for page in pages}
            await db.execute("DELETE FROM wiki_page_links WHERE source_page_id IN (SELECT id FROM wiki_pages WHERE knowledge_base_id=?)", (kb_id,))
            link_pattern = re.compile(r"\[\[([a-z0-9][a-z0-9/_-]*)(?:\|[^\]]+)?\]\]", re.IGNORECASE)
            for page in pages:
                for target_slug in set(link_pattern.findall(page["content"])):
                    await db.execute("INSERT INTO wiki_page_links(source_page_id,target_page_id,target_slug) VALUES (?,?,?)",
                                     (page["id"], slug_ids.get(target_slug), target_slug))
            await db.execute("""DELETE FROM wiki_folders WHERE knowledge_base_id=?
                AND NOT EXISTS (SELECT 1 FROM wiki_pages WHERE folder_id=wiki_folders.id)
                AND NOT EXISTS (SELECT 1 FROM wiki_folders child WHERE child.parent_id=wiki_folders.id)""", (kb_id,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def apply_wiki_taxonomy(self, kb_id: str, assignments: dict[str, str]) -> None:
        db = await self.database.connect()
        stamp = now_iso()
        try:
            await db.execute("BEGIN IMMEDIATE")
            for slug, path in assignments.items():
                parent_id = None
                folder_id = None
                for name in [part.strip() for part in path.split("/")[:2] if part.strip()]:
                    if parent_id:
                        folder = await self._one(db, "SELECT id FROM wiki_folders WHERE knowledge_base_id=? AND parent_id=? AND name=?", (kb_id, parent_id, name))
                    else:
                        folder = await self._one(db, "SELECT id FROM wiki_folders WHERE knowledge_base_id=? AND parent_id IS NULL AND name=?", (kb_id, name))
                    if folder:
                        folder_id = folder["id"]
                    else:
                        folder_id = str(uuid4())
                        await db.execute("INSERT INTO wiki_folders(id,knowledge_base_id,parent_id,name,position,created_at,updated_at) VALUES (?,?,?,?,0,?,?)", (folder_id, kb_id, parent_id, name, stamp, stamp))
                    parent_id = folder_id
                await db.execute("UPDATE wiki_pages SET folder_id=?,updated_at=? WHERE knowledge_base_id=? AND slug=? AND edit_source='pipeline'", (folder_id, stamp, kb_id, slug))
            await db.execute("DELETE FROM wiki_folders WHERE knowledge_base_id=? AND NOT EXISTS (SELECT 1 FROM wiki_pages WHERE folder_id=wiki_folders.id) AND NOT EXISTS (SELECT 1 FROM wiki_folders child WHERE child.parent_id=wiki_folders.id)", (kb_id,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def wiki_stats(self, kb_id: str) -> dict:
        counts = await self.query_all(
            "SELECT page_type,COUNT(*) count FROM wiki_pages WHERE knowledge_base_id=? AND status!='archived' GROUP BY page_type",
            (kb_id,),
        )
        links = await self.query_one(
            """SELECT COUNT(*) total_links,
            SUM(CASE WHEN target_page_id IS NULL THEN 1 ELSE 0 END) dead_links
            FROM wiki_page_links WHERE source_page_id IN (SELECT id FROM wiki_pages WHERE knowledge_base_id=?)""",
            (kb_id,),
        )
        sources = await self.query_one(
            """SELECT COUNT(DISTINCT page_id) sourced_pages FROM wiki_page_sources
            WHERE page_id IN (SELECT id FROM wiki_pages WHERE knowledge_base_id=?)""", (kb_id,)
        )
        return {"total_pages": sum(row["count"] for row in counts),
                "pages_by_type": {row["page_type"]: row["count"] for row in counts},
                "total_links": links["total_links"] or 0, "dead_links": links["dead_links"] or 0,
                "sourced_pages": sources["sourced_pages"] or 0}

    async def wiki_lint(self, kb_id: str) -> list[dict]:
        issues = []
        dead = await self.query_all(
            """SELECT p.slug source_slug,l.target_slug FROM wiki_page_links l JOIN wiki_pages p ON p.id=l.source_page_id
            WHERE p.knowledge_base_id=? AND l.target_page_id IS NULL""", (kb_id,)
        )
        issues.extend({"type": "dead_link", "slug": row["source_slug"], "detail": row["target_slug"]} for row in dead)
        unsourced = await self.query_all(
            """SELECT p.slug FROM wiki_pages p WHERE p.knowledge_base_id=? AND p.page_type IN ('entity','concept','topic')
            AND NOT EXISTS (SELECT 1 FROM wiki_page_sources s WHERE s.page_id=p.id)""", (kb_id,)
        )
        issues.extend({"type": "missing_source", "slug": row["slug"], "detail": "页面没有来源证据"} for row in unsourced)
        orphaned = await self.query_all(
            """SELECT p.slug FROM wiki_pages p WHERE p.knowledge_base_id=? AND p.page_type IN ('entity','concept','topic')
            AND NOT EXISTS (SELECT 1 FROM wiki_page_links l WHERE l.source_page_id=p.id OR l.target_page_id=p.id)""", (kb_id,)
        )
        issues.extend({"type": "orphan_page", "slug": row["slug"], "detail": "页面没有任何正向或反向 Wiki 链接"} for row in orphaned)
        empty = await self.query_all(
            "SELECT slug FROM wiki_pages WHERE knowledge_base_id=? AND length(trim(content))<20", (kb_id,)
        )
        issues.extend({"type": "empty_content", "slug": row["slug"], "detail": "页面正文过短"} for row in empty)
        duplicates = await self.query_all(
            """SELECT lower(trim(title)) normalized_title,group_concat(slug) slugs FROM wiki_pages
            WHERE knowledge_base_id=? AND status!='archived' GROUP BY lower(trim(title)) HAVING COUNT(*)>1""", (kb_id,)
        )
        for row in duplicates:
            for slug in row["slugs"].split(","):
                issues.append({"type": "duplicate_title", "slug": slug, "detail": row["slugs"]})
        return issues

    async def persist_wiki_quality(self, kb_id: str, build_id: str) -> list[dict]:
        issues = await self.wiki_lint(kb_id)
        pages = {page["slug"]: page for page in await self.list_wiki_pages(kb_id)}
        stamp = now_iso()
        await self.execute(
            "UPDATE wiki_page_issues SET status='resolved',updated_at=? WHERE knowledge_base_id=? AND status='pending'",
            (stamp, kb_id),
        )
        for issue in issues:
            page = pages.get(issue["slug"])
            await self.execute(
                """INSERT INTO wiki_page_issues
                (id,knowledge_base_id,page_id,issue_type,detail,status,build_id,created_at,updated_at)
                VALUES (?,?,?,?,?,'pending',?,?,?)
                ON CONFLICT(knowledge_base_id,page_id,issue_type,detail) DO UPDATE SET
                status='pending',build_id=excluded.build_id,updated_at=excluded.updated_at""",
                (str(uuid4()), kb_id, page["id"] if page else None, issue["type"], issue["detail"], build_id, stamp, stamp),
            )
        return issues

    async def start_stage(self, knowledge_id: str, stage: str, input_summary: dict | None = None) -> int:
        stamp = now_iso()
        db = await self.database.connect()
        try:
            sql = "INSERT INTO processing_stages(knowledge_id,stage,status,started_at,input_summary) VALUES (?,?, 'running',?,?)"
            if getattr(self.database, "dialect", "sqlite") == "postgresql":
                sql += " RETURNING id"
            cursor = await db.execute(sql, (knowledge_id, stage, stamp, json.dumps(input_summary or {}, ensure_ascii=False)))
            await db.execute("UPDATE knowledges SET current_stage=?, updated_at=? WHERE id=?", (stage, stamp, knowledge_id))
            await db.commit()
            if getattr(self.database, "dialect", "sqlite") == "postgresql":
                row = await cursor.fetchone()
                return row["id"]
            return cursor.lastrowid
        finally:
            await db.close()

    async def finish_stage(self, stage_id: int, output: dict | None = None, error: tuple[str, str] | None = None) -> None:
        db = await self.database.connect()
        try:
            if error:
                await db.execute(
                    "UPDATE processing_stages SET status='failed',finished_at=?,error_code=?,error_message=? WHERE id=?",
                    (now_iso(), error[0], error[1], stage_id),
                )
            else:
                await db.execute(
                    "UPDATE processing_stages SET status='completed',finished_at=?,output_summary=? WHERE id=?",
                    (now_iso(), json.dumps(output or {}, ensure_ascii=False), stage_id),
                )
            await db.commit()
        finally:
            await db.close()

    async def recover_tasks(self, lease_seconds: int) -> list[str]:
        cutoff = (datetime.now(UTC) - timedelta(seconds=lease_seconds)).isoformat()
        db = await self.database.connect()
        try:
            await db.execute(
                "UPDATE ingestion_tasks SET status='queued',started_at=NULL,updated_at=? WHERE status='running' AND started_at<?",
                (now_iso(), cutoff),
            )
            await db.commit()
            rows = await self._all(db, "SELECT id FROM ingestion_tasks WHERE status='queued' AND available_at<=?", (now_iso(),))
            return [row["id"] for row in rows]
        finally:
            await db.close()

    async def claim_task(self, task_id: str) -> dict | None:
        stamp = now_iso()
        db = await self.database.connect()
        try:
            cursor = await db.execute(
                "UPDATE ingestion_tasks SET status='running',attempts=attempts+1,started_at=?,updated_at=? WHERE id=? AND status='queued'",
                (stamp, stamp, task_id),
            )
            await db.commit()
            if cursor.rowcount != 1:
                return None
            return await self._one(db, "SELECT * FROM ingestion_tasks WHERE id=?", (task_id,))
        finally:
            await db.close()

    async def task_succeeded(self, task_id: str) -> None:
        await self.execute("UPDATE ingestion_tasks SET status='succeeded',finished_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), task_id))

    async def task_failed(self, task: dict, error: str, retryable: bool) -> bool:
        retry = retryable and task["attempts"] < task["max_attempts"]
        status = "queued" if retry else "failed"
        available = (datetime.now(UTC) + timedelta(seconds=min(30, 2 ** task["attempts"]))).isoformat()
        await self.execute(
            "UPDATE ingestion_tasks SET status=?,available_at=?,last_error=?,finished_at=?,updated_at=? WHERE id=?",
            (status, available, error[:2000], None if retry else now_iso(), now_iso(), task["id"]),
        )
        if not retry:
            await self.execute(
                "UPDATE knowledges SET parse_status='failed',current_stage='failed',error_message=?,updated_at=? WHERE id=?",
                (error[:2000], now_iso(), task["knowledge_id"]),
            )
        return retry

    async def mark_processing(self, knowledge_id: str) -> None:
        await self.execute("UPDATE knowledges SET parse_status='processing',error_message='',updated_at=? WHERE id=?", (now_iso(), knowledge_id))

    async def replace_index(self, knowledge: dict, parser_engine: str,
                            parents: list[dict], chunks: list[dict]) -> None:
        db = await self.database.connect()
        stamp = now_iso()
        try:
            await db.execute("BEGIN IMMEDIATE")
            old = await self._all(db, "SELECT id FROM chunks WHERE knowledge_id=?", (knowledge["id"],))
            if old:
                await db.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", [(x["id"],) for x in old])
            await db.execute("DELETE FROM chunks WHERE knowledge_id=?", (knowledge["id"],))
            await db.execute("DELETE FROM parent_chunks WHERE knowledge_id=?", (knowledge["id"],))
            for item in parents:
                await db.execute(
                    """INSERT INTO parent_chunks(id,knowledge_id,knowledge_base_id,parent_index,heading_path,page_number,content,content_hash,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (item["id"], knowledge["id"], knowledge["knowledge_base_id"], item["parent_index"],
                     item["heading_path"], item.get("page_number"), item["content"], item["content_hash"], stamp),
                )
            for item in chunks:
                await db.execute(
                    """INSERT INTO chunks(id,knowledge_id,knowledge_base_id,chunk_index,heading_path,page_number,content,parent_content,parent_id,embedding,embedding_dimension,content_hash,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["id"], knowledge["id"], knowledge["knowledge_base_id"], item["chunk_index"], item["heading_path"],
                     item.get("page_number"), item["content"], None, item.get("parent_id"), json.dumps(item["embedding"]),
                     len(item["embedding"]), item["content_hash"], stamp),
                )
                await db.execute(
                    "INSERT INTO chunks_fts(chunk_id,title_tokens,heading_tokens,content_tokens) VALUES (?,?,?,?)",
                    (item["id"], item["title_tokens"], item["heading_tokens"], item["content_tokens"]),
                )
            await db.execute(
                "UPDATE knowledges SET parser_engine=?,chunk_count=?,updated_at=? WHERE id=?",
                (parser_engine, len(chunks), stamp, knowledge["id"]),
            )
            if self.vector_outbox and chunks:
                dimension = len(chunks[0]["embedding"])
                await db.execute(
                    "INSERT INTO outbox_events(id,aggregate_type,aggregate_id,event_type,payload,status,available_at,created_at) VALUES (?,?,?,?,?,'pending',?,?)",
                    (str(uuid4()), "knowledge", knowledge["id"], "vector.delete_knowledge", json.dumps({
                        "dimension": dimension, "knowledge_id": knowledge["id"],
                    }), stamp, stamp),
                )
                points = [{
                    "id": item["id"], "vector": item["embedding"],
                    "payload": {
                        "knowledge_base_id": knowledge["knowledge_base_id"],
                        "knowledge_id": knowledge["id"], "content_hash": item["content_hash"],
                    },
                } for item in chunks]
                await db.execute(
                    "INSERT INTO outbox_events(id,aggregate_type,aggregate_id,event_type,payload,status,available_at,created_at) VALUES (?,?,?,?,?,'pending',?,?)",
                    (str(uuid4()), "knowledge", knowledge["id"], "vector.upsert", json.dumps({
                        "dimension": dimension, "points": points,
                    }), stamp, stamp),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def mark_completed(self, knowledge_id: str) -> None:
        stamp = now_iso()
        await self.execute(
            "UPDATE knowledges SET parse_status='completed',current_stage='completed',processed_at=?,updated_at=? WHERE id=?",
            (stamp, stamp, knowledge_id),
        )

    async def candidate_chunks(self, kb_ids: list[str], knowledge_ids: list[str] | None = None) -> list[dict]:
        where, params = self._scope(kb_ids, knowledge_ids)
        return await self.query_all(
            f"""SELECT c.*,COALESCE(p.content,c.parent_content) parent_content,k.title knowledge_title
            FROM chunks c LEFT JOIN parent_chunks p ON p.id=c.parent_id JOIN knowledges k ON k.id=c.knowledge_id
            WHERE {where} AND k.parse_status='completed'""",
            params,
        )

    async def chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        if not chunk_ids:
            return []
        marks = ",".join("?" for _ in chunk_ids)
        return await self.query_all(
            f"""SELECT c.*,COALESCE(p.content,c.parent_content) parent_content,k.title knowledge_title
            FROM chunks c LEFT JOIN parent_chunks p ON p.id=c.parent_id
            JOIN knowledges k ON k.id=c.knowledge_id WHERE c.id IN ({marks}) AND k.parse_status='completed'""",
            tuple(chunk_ids),
        )

    async def claim_outbox(self, limit: int) -> list[dict]:
        stamp = now_iso()
        rows = await self.query_all(
            "SELECT * FROM outbox_events WHERE status IN ('pending','failed') AND available_at<=? ORDER BY created_at LIMIT ?",
            (stamp, limit),
        )
        claimed = []
        for row in rows:
            db = await self.database.connect()
            try:
                cursor = await db.execute(
                    "UPDATE outbox_events SET status='processing',attempts=attempts+1 WHERE id=? AND status IN ('pending','failed')",
                    (row["id"],),
                )
                await db.commit()
                if cursor.rowcount == 1:
                    claimed.append(row)
            finally:
                await db.close()
        return claimed

    async def recover_outbox(self) -> None:
        await self.execute(
            "UPDATE outbox_events SET status='failed',available_at=? WHERE status='processing'",
            (now_iso(),),
        )

    async def complete_outbox(self, event_id: str) -> None:
        await self.execute(
            "UPDATE outbox_events SET status='completed',processed_at=?,last_error='' WHERE id=?",
            (now_iso(), event_id),
        )

    async def fail_outbox(self, event_id: str, error: str) -> None:
        available_at = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        await self.execute(
            "UPDATE outbox_events SET status='failed',available_at=?,last_error=? WHERE id=?",
            (available_at, error[:2000], event_id),
        )

    async def sparse_search(self, query_tokens: str, kb_ids: list[str], knowledge_ids: list[str] | None, limit: int) -> list[dict]:
        where, params = self._scope(kb_ids, knowledge_ids, alias="c")
        if getattr(self.database, "dialect", "sqlite") == "postgresql":
            sql = f"""SELECT c.*,COALESCE(p.content,c.parent_content) parent_content,k.title knowledge_title,
            ts_rank_cd(f.search_vector, plainto_tsquery('simple', ?)) bm25_score
            FROM chunks_fts f JOIN chunks c ON c.id=f.chunk_id
            LEFT JOIN parent_chunks p ON p.id=c.parent_id JOIN knowledges k ON k.id=c.knowledge_id
            WHERE f.search_vector @@ plainto_tsquery('simple', ?) AND {where}
              AND k.parse_status='completed' ORDER BY bm25_score DESC LIMIT ?"""
            return await self.query_all(sql, (query_tokens, query_tokens, *params, limit))
        sql = f"""SELECT c.*,COALESCE(p.content,c.parent_content) parent_content,k.title knowledge_title,
        bm25(chunks_fts,0,1.8,1.0) bm25_score FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.chunk_id
        LEFT JOIN parent_chunks p ON p.id=c.parent_id JOIN knowledges k ON k.id=c.knowledge_id
        WHERE chunks_fts MATCH ? AND {where} AND k.parse_status='completed' ORDER BY bm25_score LIMIT ?"""
        return await self.query_all(sql, (query_tokens, *params, limit))

    async def save_message(self, session_id: str, role: str, content: str, references=None) -> str:
        mid = str(uuid4())
        await self.execute(
            "INSERT INTO chat_messages(id,session_id,role,content,references_json,created_at) VALUES (?,?,?,?,?,?)",
            (mid, session_id, role, content, json.dumps(references, ensure_ascii=False) if references is not None else None, now_iso()),
        )
        return mid

    async def chat_history(self, session_id: str, limit: int = 6) -> list[dict]:
        rows = await self.query_all(
            "SELECT * FROM (SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at DESC LIMIT ?) ORDER BY created_at",
            (session_id, limit),
        )
        return rows

    async def clear_chat_history(self, session_id: str) -> None:
        await self.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))

    async def delete_knowledge(self, knowledge_id: str) -> None:
        row = await self.get_knowledge(knowledge_id)
        if not row:
            return
        db = await self.database.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            impacted = await self._all(
                db,
                """SELECT DISTINCT page_id FROM wiki_page_sources WHERE knowledge_id=?
                UNION SELECT DISTINCT page_id FROM wiki_page_contributions WHERE knowledge_id=?""",
                (knowledge_id, knowledge_id),
            )
            chunks = await self._all(db, "SELECT id,embedding_dimension FROM chunks WHERE knowledge_id=?", (knowledge_id,))
            if self.vector_outbox and chunks:
                stamp = now_iso()
                await db.execute(
                    "INSERT INTO outbox_events(id,aggregate_type,aggregate_id,event_type,payload,status,available_at,created_at) VALUES (?,?,?,?,?,'pending',?,?)",
                    (str(uuid4()), "knowledge", knowledge_id, "vector.delete_knowledge", json.dumps({
                        "dimension": chunks[0]["embedding_dimension"], "knowledge_id": knowledge_id,
                    }), stamp, stamp),
                )
            if chunks:
                await db.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", [(x["id"],) for x in chunks])
            await db.execute("DELETE FROM knowledges WHERE id=?", (knowledge_id,))
            await self._refresh_pipeline_pages(db, [item["page_id"] for item in impacted], now_iso())
            for item in impacted:
                await db.execute(
                    """DELETE FROM wiki_pages WHERE id=? AND edit_source='pipeline'
                    AND NOT EXISTS (SELECT 1 FROM wiki_page_sources WHERE page_id=wiki_pages.id)""",
                    (item["page_id"],),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        await self.finalize_wiki(row["knowledge_base_id"])

    async def clear_knowledge_base(self, kb_id: str) -> int:
        db = await self.database.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            knowledge_rows = await self._all(db, """SELECT k.id,MAX(c.embedding_dimension) embedding_dimension
                FROM knowledges k LEFT JOIN chunks c ON c.knowledge_id=k.id
                WHERE k.knowledge_base_id=? GROUP BY k.id""", (kb_id,))
            chunk_rows = await self._all(db, "SELECT id FROM chunks WHERE knowledge_base_id=?", (kb_id,))
            if self.vector_outbox:
                stamp = now_iso()
                for knowledge in knowledge_rows:
                    if knowledge["embedding_dimension"]:
                        await db.execute(
                            "INSERT INTO outbox_events(id,aggregate_type,aggregate_id,event_type,payload,status,available_at,created_at) VALUES (?,?,?,?,?,'pending',?,?)",
                            (str(uuid4()), "knowledge", knowledge["id"], "vector.delete_knowledge", json.dumps({
                                "dimension": knowledge["embedding_dimension"], "knowledge_id": knowledge["id"],
                            }), stamp, stamp),
                        )
            if chunk_rows:
                await db.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", [(row["id"],) for row in chunk_rows])
            await db.execute("DELETE FROM knowledges WHERE knowledge_base_id=?", (kb_id,))
            await db.execute(
                """DELETE FROM wiki_pages WHERE knowledge_base_id=? AND edit_source='pipeline' AND page_type!='index'
                AND NOT EXISTS (SELECT 1 FROM wiki_page_sources WHERE page_id=wiki_pages.id)""",
                (kb_id,),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        await self.finalize_wiki(kb_id)
        return len(knowledge_rows)

    async def execute(self, sql: str, params=()) -> None:
        db = await self.database.connect()
        try:
            await db.execute(sql, params)
            await db.commit()
        finally:
            await db.close()

    async def query_one(self, sql: str, params=()) -> dict | None:
        db = await self.database.connect()
        try:
            return await self._one(db, sql, params)
        finally:
            await db.close()

    async def query_all(self, sql: str, params=()) -> list[dict]:
        db = await self.database.connect()
        try:
            return await self._all(db, sql, params)
        finally:
            await db.close()

    @staticmethod
    async def _one(db, sql, params=()):
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None

    @staticmethod
    async def _all(db, sql, params=()):
        cursor = await db.execute(sql, params)
        return [dict(x) for x in await cursor.fetchall()]

    @staticmethod
    def _scope(kb_ids: list[str], knowledge_ids: list[str] | None, alias: str = "c") -> tuple[str, tuple]:
        clauses = [f"{alias}.knowledge_base_id IN ({','.join('?' for _ in kb_ids)})"]
        params: list[str] = list(kb_ids)
        if knowledge_ids:
            clauses.append(f"{alias}.knowledge_id IN ({','.join('?' for _ in knowledge_ids)})")
            params.extend(knowledge_ids)
        return " AND ".join(clauses), tuple(params)
