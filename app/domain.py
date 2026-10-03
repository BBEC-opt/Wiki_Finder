from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ParseWarning:
    code: str
    message: str
    page_number: int | None = None


@dataclass
class ParsedImage:
    id: str
    original_name: str
    mime_type: str
    storage_path: str
    markdown_ref: str
    page_number: int | None = None
    width: int | None = None
    height: int | None = None
    content_hash: str = ""
    caption: str = ""
    ocr_text: str = ""


@dataclass
class ParsedTable:
    id: str
    markdown: str
    row_count: int
    column_count: int
    page_number: int | None = None


@dataclass
class ParsedPage:
    page_number: int
    markdown: str
    start_offset: int = 0
    end_offset: int = 0


@dataclass
class ParsedDocument:
    markdown: str
    parser_engine: str
    metadata: dict[str, Any] = field(default_factory=dict)
    pages: list[ParsedPage] = field(default_factory=list)
    images: list[ParsedImage] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    duration_ms: int = 0

    def manifest(self) -> dict[str, Any]:
        return {
            "parser_engine": self.parser_engine,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
            "pages": [asdict(x) for x in self.pages],
            "images": [asdict(x) for x in self.images],
            "tables": [asdict(x) for x in self.tables],
            "warnings": [asdict(x) for x in self.warnings],
            "quality": {
                "page_count": len(self.pages),
                "nonempty_page_count": sum(bool(x.markdown.strip()) for x in self.pages),
                "text_char_count": len(self.markdown),
                "image_count": len(self.images),
                "table_count": len(self.tables),
                "warning_count": len(self.warnings),
            },
        }


@dataclass
class ChunkDraft:
    chunk_index: int
    content: str
    heading_path: str
    page_number: int | None
    parent_content: str | None = None
    id: str | None = None
    parent_index: int | None = None


@dataclass
class ParentChunkDraft:
    parent_index: int
    content: str
    heading_path: str
    page_number: int | None
    id: str | None = None
