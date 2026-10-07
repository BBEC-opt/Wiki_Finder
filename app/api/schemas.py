"""API 请求与响应模型。"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    chunk_size: int | None = Field(None, ge=100, le=10_000)
    chunk_overlap: int | None = Field(None, ge=0, le=2_000)
    parent_chunk_size: int | None = Field(None, ge=200, le=40_000)

    @model_validator(mode="after")
    def check_overlap(self):
        if self.chunk_size is not None and self.chunk_overlap is not None and self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        if self.chunk_size is not None and self.parent_chunk_size is not None and self.parent_chunk_size < self.chunk_size:
            raise ValueError("parent_chunk_size 必须大于等于 chunk_size")
        return self


class ManualKnowledgeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2_000_000)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)
    knowledge_base_ids: list[str] = Field(min_length=1)
    knowledge_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(5, ge=1, le=20)
    candidate_k: int = Field(20, ge=1, le=100)
    score_threshold: float = Field(0.0, ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def check_candidates(self):
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k 必须大于等于 top_k")
        return self


class ChatRequest(SearchRequest):
    session_id: str = Field(min_length=1, max_length=100)
    retry_message_id: str | None = Field(None, max_length=100)

    @model_validator(mode="after")
    def strip_query(self):
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("问题不能为空")
        return self


class SessionCreate(BaseModel):
    knowledge_base_id: str
    knowledge_ids: list[str] = Field(default_factory=list, max_length=100)
    title: str = Field(default="新会话", min_length=1, max_length=100)


class SessionUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ChatSession(BaseModel):
    id: str
    title: str
    knowledge_base_id: str | None = None
    knowledge_ids: list[str]
    active_turn: str | None = None
    created_at: str
    updated_at: str


class Health(BaseModel):
    status: Literal["live", "ready"]


class KnowledgeBase(BaseModel):
    id: str
    name: str
    description: str
    chunk_size: int
    chunk_overlap: int
    parent_chunk_size: int
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int
    created_at: str
    updated_at: str


class Knowledge(BaseModel):
    id: str
    knowledge_base_id: str
    title: str
    source_type: Literal["file", "manual"]
    file_name: str | None = None
    parse_status: Literal["pending", "processing", "completed", "failed"]
    current_stage: str
    parser_engine: str | None = None
    chunk_count: int = 0
    error_message: str = ""
    created_at: str
    updated_at: str
    processed_at: str | None = None


class Chunk(BaseModel):
    id: str
    knowledge_id: str
    knowledge_base_id: str
    chunk_index: int
    heading_path: str
    page_number: int | None = None
    content: str
    parent_id: str | None = None
    parent_index: int | None = None
    parent_content: str | None = None
    embedding_dimension: int
    content_hash: str
    created_at: str


class ProcessingStage(BaseModel):
    id: int
    knowledge_id: str
    stage: str
    status: str
    started_at: str
    finished_at: str | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class SearchResult(BaseModel):
    index: int
    chunk_id: str
    knowledge_id: str
    knowledge_base_id: str
    knowledge_title: str
    chunk_index: int
    heading_path: str
    page_number: int | None = None
    content: str
    content_hash: str
    context_content: str
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    bm25_score: float | None = None
    fusion_score: float


class ChatMessage(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    references_json: str | None = None
    turn_id: str | None = None
    status: str = "completed"
    created_at: str


class WikiPage(BaseModel):
    id: str
    knowledge_base_id: str
    knowledge_id: str | None = None
    slug: str
    title: str
    summary: str
    content: str
    page_type: Literal["summary", "topic", "source", "concept", "entity", "index"]
    status: Literal["draft", "published", "archived"] = "published"
    folder_id: str | None = None
    edit_source: Literal["pipeline", "user"] = "pipeline"
    version: int = 1
    position: int
    source_refs: list[dict[str, Any]]
    created_at: str
    updated_at: str


class WikiPageWrite(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9/_-]*$", max_length=255)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=1000)
    content: str = Field(min_length=1, max_length=2_000_000)
    page_type: Literal["topic", "concept", "entity"] = "topic"
    status: Literal["draft", "published", "archived"] = "published"
    folder_id: str | None = None


class WikiFolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    parent_id: str | None = None


class WikiFolder(BaseModel):
    id: str
    knowledge_base_id: str
    parent_id: str | None = None
    name: str
    position: int
    created_at: str
    updated_at: str


class Deleted(BaseModel):
    deleted: bool


class Cleared(BaseModel):
    cleared: bool
    deleted_count: int


class ParserPreview(BaseModel):
    markdown: str
    manifest: dict[str, Any]


class SuccessResponse(BaseModel):
    success: Literal[True] = True


class HealthResponse(SuccessResponse):
    data: Health


class KnowledgeBaseResponse(SuccessResponse):
    data: KnowledgeBase


class KnowledgeBaseListResponse(SuccessResponse):
    data: list[KnowledgeBase]


class KnowledgeResponse(SuccessResponse):
    data: Knowledge


class KnowledgeListResponse(SuccessResponse):
    data: list[Knowledge]


class ChunkListResponse(SuccessResponse):
    data: list[Chunk]


class StageListResponse(SuccessResponse):
    data: list[ProcessingStage]


class SearchResponse(SuccessResponse):
    data: list[SearchResult]


class MessageListResponse(SuccessResponse):
    data: list[ChatMessage]


class WikiPageResponse(SuccessResponse):
    data: WikiPage


class WikiPageListResponse(SuccessResponse):
    data: list[WikiPage]


class WikiFolderListResponse(SuccessResponse):
    data: list[WikiFolder]


class DeletedResponse(SuccessResponse):
    data: Deleted


class ClearedResponse(SuccessResponse):
    data: Cleared


class ParserPreviewResponse(SuccessResponse):
    data: ParserPreview


class ArtifactResponse(SuccessResponse):
    data: dict[str, Any]


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    error: ErrorDetail


class SessionResponse(SuccessResponse):
    data: ChatSession


class SessionListResponse(SuccessResponse):
    data: list[ChatSession]


class StopResult(BaseModel):
    stopped: bool


class StopResponse(SuccessResponse):
    data: StopResult
