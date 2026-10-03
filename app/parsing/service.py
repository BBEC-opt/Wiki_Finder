import json
from pathlib import Path

from app.domain import ParsedDocument
from app.core.errors import ParseError
from app.parsing.core import ParseRequest, detect_file_type, normalize_markdown
from app.parsing.parsers import BuiltinParser


class ParsingService:
    def __init__(self, artifact_root: Path, ocr_engine: str = "none"):
        self.artifact_root = artifact_root
        self.parser = BuiltinParser(ocr_engine)

    async def parse(self, path: Path, knowledge_id: str, title: str = "") -> ParsedDocument:
        file_type, detection = detect_file_type(path)
        artifact_dir = self.artifact_root / knowledge_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if file_type not in self.parser.supported_types:
            raise ParseError("unsupported_file_type", f"不支持的文件类型：{file_type}")
        request = ParseRequest(path, knowledge_id, artifact_dir, file_type, title)
        result = await self.parser.parse(request, file_type)
        result.markdown = normalize_markdown(result.markdown)
        result.metadata.update(detection)
        result.metadata["selected_engine"] = self.parser.name
        self.validate(result)
        (artifact_dir / "parsed.md").write_text(result.markdown, encoding="utf-8")
        (artifact_dir / "manifest.json").write_text(
            json.dumps(result.manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    @staticmethod
    def validate(result: ParsedDocument) -> None:
        visible = result.markdown.strip()
        if not visible:
            raise ParseError("empty_document", "文档解析结果为空")
        replacement_ratio = visible.count("\ufffd") / max(1, len(visible))
        if replacement_ratio > 0.02:
            raise ParseError("garbled_document", "解析结果包含过多乱码")
