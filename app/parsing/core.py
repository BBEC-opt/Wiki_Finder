import hashlib
import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ParseError


OFFICE_SIGNATURES = {
    "word/": "docx",
    "xl/": "xlsx",
    "ppt/": "pptx",
}


@dataclass
class ParseRequest:
    path: Path
    knowledge_id: str
    artifact_dir: Path
    declared_type: str = ""
    title: str = ""


def detect_file_type(path: Path, declared: str = "") -> tuple[str, dict]:
    head = path.read_bytes()[:8192]
    suffix = path.suffix.lower().lstrip(".")
    detected = suffix
    confidence = "extension"
    if head.startswith(b"%PDF"):
        detected, confidence = "pdf", "magic"
    elif head.startswith(b"PK\x03\x04"):
        import zipfile
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
            for prefix, kind in OFFICE_SIGNATURES.items():
                if any(x.startswith(prefix) for x in names):
                    detected, confidence = kind, "container"
                    break
        except zipfile.BadZipFile as exc:
            raise ParseError("corrupt_archive", "Office 文档容器已损坏") from exc
    elif head[:8].startswith(b"\x89PNG"):
        detected, confidence = "png", "magic"
    elif head.startswith(b"\xff\xd8\xff"):
        detected, confidence = "jpg", "magic"
    elif head.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        detected, confidence = "html", "content"
    elif not detected:
        detected, confidence = "txt", "content"
    declared = declared.lower().lstrip(".")
    return detected, {
        "declared_type": declared or suffix,
        "detected_type": detected,
        "detection_confidence": confidence,
        "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "sha256": sha256_file(path),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_markdown(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    text = re.sub(r"(?m)^(#{1,6})([^ #\n])", r"\1 \2", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def markdown_table(rows: list[list[object]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = []
    for row in rows:
        values = [str(x or "").replace("\n", "<br>").replace("|", "\\|") for x in row]
        normalized.append(values + [""] * (width - len(values)))
    header = normalized[0]
    if not any(x.strip() for x in header):
        header = [f"列{i + 1}" for i in range(width)]
    header = [x or f"列{i + 1}" for i, x in enumerate(header)]
    body = normalized[1:]
    return "\n".join([
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
        *("| " + " | ".join(row) + " |" for row in body),
    ])
