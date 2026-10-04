"""知识库级 Wiki Map-Reduce 生成管道。"""

import json
import re
import unicodedata
from pathlib import Path
from uuid import uuid4

from app.infrastructure.tracing import current_observation, traced
from app.domain import ChunkDraft, ParsedDocument


PAGE_MARKER = re.compile(r"<!--\s*page:(\d+)\s*-->")
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)

CANDIDATE_SYSTEM = """你是知识库 Wiki 的候选条目编辑。只依据输入文档抽取被实质讨论、值得独立成页的实体与概念。
输出严格 JSON，不要代码围栏或解释：{"candidates":[{"slug":"concept/example","title":"标题","page_type":"concept"}]}。
slug 只能含小写字母、数字、斜杠、连字符；页面类型只能是 entity 或 concept。此阶段只建立候选骨架，不得输出引用。
仅偶然提及、章节名过于宽泛、缺少可陈述事实的词语不得入选；同一对象的简称、全称和别名只保留一个。相关不等于同一条目，已有同义页面必须复用其 slug。
granularity=focused 时只保留贯穿文档的核心对象；standard 保留有完整段落说明的对象；exhaustive 可保留局部但有充分说明的对象。"""

CITATION_SYSTEM = """你负责为既有 Wiki 候选逐 Chunk 标注证据。只在 Chunk 对候选有实质性陈述时引用，名称偶然出现不算证据。
输出严格 JSON：{"citations":{"concept/example":[0,2]}}。键只能来自候选 slug，值只能使用本批输入的 chunk index；没有证据时返回空数组。"""

DEDUP_SYSTEM = """判断候选 Wiki 条目与已有页面是否指向同一事物。相关不等于相同；只有名称变体、别名或明确同义时才能合并。
输出严格 JSON：{"merges":{"candidate/slug":"existing/slug"}}。没有合并则返回空对象。"""

TAXONOMY_SYSTEM = """为一批 Wiki 候选规划简洁稳定的目录。优先复用已有目录，目录最多两级，不要把 entity/concept 等类型名作为目录。
输出严格 JSON：{"folders":{"slug":"一级/二级"}}。每个 slug 必须有目录。"""

PAGE_SYSTEM = """你是严谨的 Wiki 编辑。只根据 <evidence> 撰写页面，不得引入外部事实。
输出严格 JSON，不要代码围栏或解释：{"summary":"一句摘要","content":"Markdown 正文"}。
正文应先给定义，再按证据组织关键事实；不同来源有冲突时明确并列，不擅自裁决；适合时使用给定的 [[slug|标题]] 内链；不要输出来源中没有的信息。"""


class WikiGenerationService:
    def __init__(self, chat_provider=None, provider_mode: str = "offline",
                 extraction_granularity: str = "standard", max_candidates: int = 12):
        self.chat = chat_provider
        self.provider_mode = provider_mode
        self.extraction_granularity = extraction_granularity
        self.max_candidates = max_candidates

    @traced("wiki-generation")
    async def generate(
        self, knowledge: dict, parsed: ParsedDocument, chunks: list[ChunkDraft], existing_pages: list[dict] | None = None,
        repo=None, build_id: str | None = None,
    ) -> tuple[list[dict], dict]:
        current_observation().update(input={"knowledge_id": knowledge["id"], "chunks": len(chunks)})
        existing_pages = existing_pages or []
        await self._stage_start(repo, build_id, "candidate_extraction", {"chunks": len(chunks)})
        candidates, used_model = await self._candidates(knowledge, chunks, existing_pages)
        await self._stage_finish(repo, build_id, "candidate_extraction", {"candidates": len(candidates)})
        await self._stage_start(repo, build_id, "citation_mapping", {"candidates": len(candidates), "chunks": len(chunks)})
        candidates = await self._map_citations(candidates, chunks)
        await self._stage_finish(repo, build_id, "citation_mapping", {"candidates_with_evidence": len(candidates), "citations": sum(len(c["chunk_indices"]) for c in candidates)})
        await self._stage_start(repo, build_id, "deduplication", {"candidates": len(candidates), "existing_pages": len(existing_pages)})
        candidates = await self._deduplicate(candidates, existing_pages)
        await self._stage_finish(repo, build_id, "deduplication", {"candidates": len(candidates)})
        await self._stage_start(repo, build_id, "taxonomy", {"candidates": len(candidates)})
        taxonomy = await self._taxonomy(candidates, existing_pages)
        await self._stage_finish(repo, build_id, "taxonomy", {"folders": len(set(taxonomy.values())), "pages": len(taxonomy)})
        await self._stage_start(repo, build_id, "reduce", {"candidates": len(candidates)})
        pages = [self._summary_page(knowledge, parsed, chunks)]
        for candidate in candidates:
            page = await self._candidate_page(knowledge, candidate, chunks, existing_pages)
            pages.append(page)
        pages.append(self._source_page(knowledge, parsed))
        await self._stage_finish(repo, build_id, "reduce", {"pages": len(pages)})
        current_observation().update(output={"mode": "model" if used_model else "offline", "pages": len(pages), "candidates": len(candidates)})
        return pages, {
            "mode": "model" if used_model else "offline",
            "candidates": len(candidates),
            "cited_chunks": len({i for item in candidates for i in item["chunk_indices"]}),
            "phases": ["candidate_extraction", "citation_mapping", "deduplication", "taxonomy", "reduce", "finalize"],
            "taxonomy": taxonomy,
        }

    @staticmethod
    async def _stage_start(repo, build_id: str | None, stage: str, inputs: dict) -> None:
        if repo and build_id:
            await repo.start_wiki_build_stage(build_id, stage, inputs)

    @staticmethod
    async def _stage_finish(repo, build_id: str | None, stage: str, output: dict) -> None:
        if repo and build_id:
            await repo.finish_wiki_build_stage(build_id, stage, output)

    @traced("wiki-deduplication")
    async def _deduplicate(self, candidates: list[dict], existing: list[dict]) -> list[dict]:
        eligible = [page for page in existing
                    if page["page_type"] in {"entity", "concept", "topic"} and page["status"] != "archived"]
        # 人工页面优先成为规范页，避免自动页抢占同名条目。
        eligible.sort(key=lambda page: page.get("edit_source") != "user")
        by_title = {self._dedup_key(page["title"]): page for page in reversed(eligible)}
        canonical = {page["slug"]: page for page in eligible}
        for item in candidates:
            match = by_title.get(self._dedup_key(item["title"]))
            if match:
                self._adopt_canonical(item, match)
        if self.provider_mode == "openai" and self.chat and candidates and eligible:
            payload = {"candidates": [{"slug": c["slug"], "title": c["title"]} for c in candidates],
                       "existing": [{"slug": p["slug"], "title": p["title"], "summary": p["summary"]} for p in eligible[:150]]}
            try:
                raw = await self.chat.complete([{"role": "system", "content": DEDUP_SYSTEM},
                                                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
                merges = self._json(raw).get("merges", {})
                for item in candidates:
                    target = merges.get(item["slug"])
                    if target in canonical:
                        self._adopt_canonical(item, canonical[target])
            except Exception as exc:
                current_observation().update(level="WARNING", status_message=type(exc).__name__, metadata={"fallback": "deterministic"})
                pass
        merged: dict[str, dict] = {}
        for item in candidates:
            key = item["slug"] if item["slug"] in canonical else self._dedup_key(item["title"])
            if key in merged:
                merged[key]["chunk_indices"] = sorted(set(merged[key]["chunk_indices"] + item["chunk_indices"]))
            else:
                merged[key] = item
        return list(merged.values())

    @classmethod
    def _adopt_canonical(cls, candidate: dict, page: dict) -> None:
        candidate["slug"] = page["slug"]
        candidate["title"] = page["title"]
        candidate["page_type"] = page["page_type"] if page["page_type"] in {"entity", "concept", "topic"} else candidate["page_type"]
        if page.get("folder_path"):
            candidate["folder"] = page["folder_path"]

    @classmethod
    def _dedup_key(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", cls._plain(value)).casefold()
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value)

    @traced("wiki-taxonomy")
    async def _taxonomy(self, candidates: list[dict], existing: list[dict]) -> dict[str, str]:
        entries = {page["slug"]: {"slug": page["slug"], "title": page["title"], "type": page["page_type"]}
                   for page in existing if page["page_type"] in {"entity", "concept", "topic"}}
        for item in candidates:
            item["folder"] = "实体" if item["page_type"] == "entity" else "主题"
            entries[item["slug"]] = {"slug": item["slug"], "title": item["title"], "type": item["page_type"]}
        assignments = {page["slug"]: (page.get("folder_path") or ("实体" if page["page_type"] == "entity" else "主题"))
                       for page in existing if page["slug"] in entries}
        assignments.update({item["slug"]: item["folder"] for item in candidates})
        if self.provider_mode != "openai" or not self.chat or not candidates:
            return assignments
        existing_folders = sorted({str(page.get("folder_path") or "") for page in existing if page.get("folder_path")})
        try:
            raw = await self.chat.complete([{"role": "system", "content": TAXONOMY_SYSTEM}, {"role": "user", "content": json.dumps({
                "candidates": list(entries.values()),
                "existing_folders": existing_folders,
            }, ensure_ascii=False)}])
            folders = self._json(raw).get("folders", {})
            for slug in assignments:
                path = str(folders.get(slug, assignments[slug])).strip(" / ")
                if path and len(path.split("/")) <= 2:
                    assignments[slug] = path[:100]
        except Exception as exc:
            current_observation().update(level="WARNING", status_message=type(exc).__name__, metadata={"fallback": "deterministic"})
            pass
        for item in candidates:
            item["folder"] = assignments[item["slug"]]
        return assignments

    @traced("wiki-candidates")
    async def _candidates(self, knowledge: dict, chunks: list[ChunkDraft], existing: list[dict]) -> tuple[list[dict], bool]:
        if self.provider_mode == "openai" and self.chat:
            known = [{"slug": p["slug"], "title": p["title"], "type": p["page_type"]}
                     for p in existing if p["page_type"] in {"entity", "concept", "topic"}][:100]
            collected = []
            for offset in range(0, len(chunks), 8):
                try:
                    compact = [{"index": c.chunk_index, "heading": c.heading_path, "content": c.content} for c in chunks[offset:offset + 8]]
                    prompt = f"<granularity>{self.extraction_granularity}</granularity>\n<existing>{json.dumps(known, ensure_ascii=False)}</existing>\n<chunks>{json.dumps(compact, ensure_ascii=False)}</chunks>"
                    raw = await self.chat.complete([{"role": "system", "content": CANDIDATE_SYSTEM}, {"role": "user", "content": prompt}])
                    collected.extend(self._json(raw).get("candidates", []))
                except Exception as exc:
                    current_observation().update(level="WARNING", status_message=type(exc).__name__, metadata={"fallback": "deterministic"})
                    # 单批失败不丢弃其他批次已经得到的有效候选。
                    continue
            candidates = self._rank_candidate_skeletons(collected, self._candidate_limit())
            if candidates:
                return candidates, True
        candidates = self._offline_candidates(knowledge, chunks)
        for candidate in candidates:
            candidate.pop("chunk_indices", None)
        return candidates, False

    def _candidate_limit(self) -> int:
        ratios = {"focused": 0.5, "standard": 1.0, "exhaustive": 1.0}
        return max(1, round(self.max_candidates * ratios.get(self.extraction_granularity, 1.0)))

    @classmethod
    def _rank_candidate_skeletons(cls, items: list, max_candidates: int) -> list[dict]:
        """跨批聚合同一对象，优先保留被多个文档片段重复识别的候选。"""
        grouped: dict[str, dict] = {}
        for position, raw in enumerate(items):
            validated = cls._validate_candidate_skeletons([raw], 1)
            if not validated:
                continue
            item = validated[0]
            key = cls._dedup_key(item["title"])
            if not key:
                continue
            if key not in grouped:
                grouped[key] = {"item": item, "mentions": 0, "position": position}
            grouped[key]["mentions"] += 1
        ranked = sorted(grouped.values(), key=lambda entry: (-entry["mentions"], entry["position"]))
        return [entry["item"] for entry in ranked[:max_candidates]]

    @traced("wiki-citations")
    async def _map_citations(self, candidates: list[dict], chunks: list[ChunkDraft]) -> list[dict]:
        if not candidates:
            return []
        citations = {candidate["slug"]: set() for candidate in candidates}
        if self.provider_mode == "openai" and self.chat:
            try:
                skeletons = [{"slug": c["slug"], "title": c["title"], "type": c["page_type"]} for c in candidates]
                for offset in range(0, len(chunks), 8):
                    batch = chunks[offset:offset + 8]
                    payload = {"candidates": skeletons, "chunks": [{"index": c.chunk_index, "heading": c.heading_path, "content": c.content} for c in batch]}
                    raw = await self.chat.complete([{"role": "system", "content": CITATION_SYSTEM}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
                    mapping = self._json(raw).get("citations", {})
                    valid = {c.chunk_index for c in batch}
                    for slug in citations:
                        citations[slug].update(int(i) for i in mapping.get(slug, []) if str(i).isdigit() and int(i) in valid)
            except Exception as exc:
                current_observation().update(level="WARNING", status_message=type(exc).__name__, metadata={"fallback": "deterministic"})
                citations = {candidate["slug"]: set() for candidate in candidates}
        for candidate in candidates:
            if not citations[candidate["slug"]]:
                title = self._plain(candidate["title"]).lower()
                for chunk in chunks:
                    haystack = self._plain(f"{chunk.heading_path} {chunk.content}").lower()
                    if title and title in haystack:
                        citations[candidate["slug"]].add(chunk.chunk_index)
        result = []
        for candidate in candidates:
            indices = sorted(citations[candidate["slug"]])
            if indices:
                candidate["chunk_indices"] = indices
                result.append(candidate)
        return result

    @traced("wiki-page")
    async def _candidate_page(self, knowledge: dict, candidate: dict, chunks: list[ChunkDraft], existing: list[dict]) -> dict:
        chunk_map = {chunk.chunk_index: chunk for chunk in chunks}
        evidence = [chunk_map[i] for i in candidate["chunk_indices"] if i in chunk_map]
        previous = next((p for p in existing if p["slug"] == candidate["slug"]), None)
        contribution_summary = self._summary(" ".join(c.content for c in evidence))
        contribution_content = self._contribution_content(evidence)
        prior_contributions = [item for item in (previous.get("contributions", []) if previous else [])
                               if item["knowledge_id"] != knowledge["id"]]
        contributions = [*prior_contributions, {
            "knowledge_id": knowledge["id"], "knowledge_title": knowledge["title"],
            "summary": contribution_summary, "content": contribution_content,
            "chunk_ids": [chunk.id for chunk in evidence if chunk.id],
        }]
        summary = self._summary(" ".join(item["summary"] for item in contributions))
        content = self._aggregate_offline(candidate["title"], contributions)
        if self.provider_mode == "openai" and self.chat and evidence:
            related = [{"slug": p["slug"], "title": p["title"]} for p in existing
                       if p["slug"] != candidate["slug"] and p["page_type"] not in {"source", "summary"}][:40]
            payload = {
                "page": {"slug": candidate["slug"], "title": candidate["title"]},
                "related_pages": related,
                "contributions": contributions,
            }
            try:
                raw = await self.chat.complete([{"role": "system", "content": PAGE_SYSTEM},
                                                {"role": "user", "content": f"<evidence>{json.dumps(payload, ensure_ascii=False)}</evidence>"}])
                result = self._json(raw)
                if str(result.get("content", "")).strip():
                    summary = str(result.get("summary") or summary)[:1000]
                    content = str(result["content"]).strip()
            except Exception as exc:
                current_observation().update(level="WARNING", status_message=type(exc).__name__, metadata={"fallback": "deterministic"})
                pass
        page = self._page(candidate["slug"], candidate["title"], summary, content, candidate["page_type"],
                          candidate["chunk_indices"], knowledge, candidate.get("folder", "主题"),
                          [chunk.id for chunk in evidence if chunk.id])
        page["contribution"] = contributions[-1]
        return page

    def _offline_candidates(self, knowledge: dict, chunks: list[ChunkDraft]) -> list[dict]:
        found = []
        seen = set()
        for chunk in chunks:
            title = (chunk.heading_path.split(" / ")[-1] if chunk.heading_path else "").strip()
            if not title or len(self._plain(chunk.content)) < 40:
                continue
            slug = f"concept/{self._slug(title, knowledge['id'], chunk.chunk_index)}"
            if slug in seen:
                next(item for item in found if item["slug"] == slug)["chunk_indices"].append(chunk.chunk_index)
                continue
            seen.add(slug)
            found.append({"slug": slug, "title": title, "page_type": "concept",
                          "chunk_indices": [chunk.chunk_index], "folder": "主题"})
            if len(found) == self.max_candidates:
                break
        return found

    def _summary_page(self, knowledge: dict, parsed: ParsedDocument, chunks: list[ChunkDraft]) -> dict:
        title = Path(knowledge["title"]).stem or knowledge["title"]
        summary = self._summary(parsed.markdown)
        points = [self._plain(c.content)[:240] for c in chunks[:5] if self._plain(c.content)]
        content = "\n".join([f"# {title}", "", f"> {summary}", "", "## 核心内容", "",
                             *(f"- {point}" for point in points), "", "## 文档信息", "",
                             f"- 页数：{len(parsed.pages) or 1}", f"- 图片：{len(parsed.images)}", f"- 表格：{len(parsed.tables)}"])
        return self._page(f"summary/{knowledge['id']}", title, summary, content, "summary",
                          [c.chunk_index for c in chunks[:5]], knowledge, "文档", [c.id for c in chunks[:5] if c.id])

    def _source_page(self, knowledge: dict, parsed: ParsedDocument) -> dict:
        title = Path(knowledge["title"]).stem or knowledge["title"]
        return self._page(f"source/{knowledge['id']}", f"{title} · 原文", "按解析顺序保留的文档原文与页码。",
                          parsed.markdown, "source", [], knowledge, "原文")

    def _page(self, slug: str, title: str, summary: str, content: str, page_type: str,
              chunk_indices: list[int], knowledge: dict, folder: str = "主题", chunk_ids: list[str] | None = None) -> dict:
        return {"id": str(uuid4()), "slug": slug, "title": title, "summary": summary, "content": content,
                "page_type": page_type, "position": 0, "folder": folder,
                "source_refs": [{"knowledge_id": knowledge["id"], "title": knowledge["title"],
                                 "chunk_indices": sorted(set(chunk_indices)), "chunk_ids": chunk_ids or []}]}

    @staticmethod
    def _contribution_content(evidence: list[ChunkDraft]) -> str:
        parts = []
        for chunk in evidence:
            if chunk.heading_path:
                parts.extend(["", f"## {chunk.heading_path.split(' / ')[-1]}"])
            parts.extend(["", chunk.content.strip()])
        return "\n".join(parts).strip()

    @staticmethod
    def _aggregate_offline(title: str, contributions: list[dict]) -> str:
        parts = [f"# {title}"]
        for item in contributions:
            knowledge_id = item["knowledge_id"]
            parts.extend(["", f"<!-- source:{knowledge_id} -->", f"## 来源：{item['knowledge_title']}", "",
                          item["content"], f"<!-- /source:{knowledge_id} -->"])
        return "\n".join(parts)

    @staticmethod
    def _validate_candidate_skeletons(items: list, max_candidates: int = 12) -> list[dict]:
        result, seen = [], set()
        for raw in items[:max(20, max_candidates * 4)]:
            if not isinstance(raw, dict):
                continue
            slug = str(raw.get("slug", "")).strip().lower()
            title = str(raw.get("title", "")).strip()
            if not re.fullmatch(r"(?:entity|concept)/[a-z0-9][a-z0-9_-]*", slug) or not title:
                continue
            if slug in seen:
                continue
            seen.add(slug)
            result.append({"slug": slug, "title": title[:200], "page_type": raw.get("page_type") if raw.get("page_type") in {"entity", "concept"} else "concept",
                           "folder": str(raw.get("folder", "主题"))[:100]})
            if len(result) == max_candidates:
                break
        return result

    @staticmethod
    def _json(raw: str) -> dict:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("模型未返回 JSON 对象")
        value = json.loads(text[start:end + 1])
        if not isinstance(value, dict):
            raise ValueError("模型返回类型错误")
        return value

    @staticmethod
    def _summary(markdown: str) -> str:
        plain = WikiGenerationService._plain(markdown)
        if not plain:
            return "暂无可用摘要。"
        sentence = re.split(r"(?<=[。！？.!?])\s+", plain, maxsplit=1)[0]
        return sentence[:220].rstrip() + ("…" if len(sentence) > 220 else "")

    @staticmethod
    def _plain(markdown: str) -> str:
        text = PAGE_MARKER.sub(" ", markdown)
        text = MARKDOWN_IMAGE.sub(" ", text)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"[`*_>|]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _slug(title: str, knowledge_id: str, index: int) -> str:
        value = unicodedata.normalize("NFKC", title).lower()
        value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value).strip("-")
        return value[:80] or f"{knowledge_id}-{index + 1}"
