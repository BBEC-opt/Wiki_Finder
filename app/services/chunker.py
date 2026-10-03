"""Markdown 文档切分。"""

import re

from app.domain import ChunkDraft, ParentChunkDraft


PROTECTED_BLOCK = re.compile(r"(```[\s\S]*?```|\$\$[\s\S]*?\$\$|(?:^\|.*\|\s*$\n?){2,})", re.MULTILINE)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
PAGE_MARKER = re.compile(r"<!--\s*page:(\d+)\s*-->")


def split_document(markdown: str, chunk_size: int, overlap: int,
                   parent_chunk_size: int | None = None) -> list[ChunkDraft]:
    units = _structural_units(markdown)
    chunks: list[ChunkDraft] = []
    buffer: list[str] = []
    length = 0
    current_heading: list[str] = []
    current_page: int | None = None

    def emit() -> None:
        nonlocal buffer, length
        content = "\n\n".join(x for x in buffer if x.strip()).strip()
        if not content:
            buffer, length = [], 0
            return
        parent = f"{' > '.join(current_heading)}\n\n{content}" if current_heading else content
        chunks.append(ChunkDraft(len(chunks), content, " > ".join(current_heading), current_page, parent))
        tail = content[-overlap:] if overlap else ""
        buffer = [tail] if tail.strip() else []
        length = len(tail)

    for unit in units:
        page = PAGE_MARKER.search(unit)
        if page:
            current_page = int(page.group(1))
            continue
        heading = HEADING.match(unit.strip())
        if heading:
            emit()
            level, title = len(heading.group(1)), heading.group(2)
            current_heading = current_heading[: level - 1]
            current_heading.append(title)
            continue
        for piece in _split_oversized(unit, chunk_size):
            if length and length + len(piece) + 2 > chunk_size:
                emit()
            buffer.append(piece)
            length += len(piece) + 2
    emit()
    # Parent chunks follow structural section/page boundaries. A long section
    # is divided into bounded parents while children remain the only embedded
    # units.
    parent_limit = parent_chunk_size or max(chunk_size * 4, 2_000)
    groups: dict[tuple[str, int | None], list[ChunkDraft]] = {}
    for chunk in chunks:
        groups.setdefault((chunk.heading_path, chunk.page_number), []).append(chunk)
    parent_index = 0
    for group in groups.values():
        batch: list[ChunkDraft] = []
        batch_length = 0
        for item in group:
            if batch and batch_length + len(item.content) + 2 > parent_limit:
                _assign_parent(batch, parent_index)
                parent_index += 1
                batch, batch_length = [], 0
            batch.append(item)
            batch_length += len(item.content) + 2
        if batch:
            _assign_parent(batch, parent_index)
            parent_index += 1
    return chunks


def build_parent_chunks(chunks: list[ChunkDraft]) -> list[ParentChunkDraft]:
    groups: dict[int, list[ChunkDraft]] = {}
    for chunk in chunks:
        if chunk.parent_index is not None:
            groups.setdefault(chunk.parent_index, []).append(chunk)
    return [
        ParentChunkDraft(index, items[0].parent_content or items[0].content,
                         items[0].heading_path, items[0].page_number)
        for index, items in sorted(groups.items())
    ]


def _assign_parent(chunks: list[ChunkDraft], parent_index: int) -> None:
    content = "\n\n".join(item.content for item in chunks)
    for item in chunks:
        item.parent_index = parent_index
        item.parent_content = content


def _structural_units(text: str) -> list[str]:
    placeholders: dict[str, str] = {}

    def protect(match):
        key = f"\x00BLOCK{len(placeholders)}\x00"
        placeholders[key] = match.group(0).strip()
        return f"\n\n{key}\n\n"

    protected = PROTECTED_BLOCK.sub(protect, text)
    units = []
    for part in re.split(r"\n\s*\n", protected):
        part = part.strip()
        if not part:
            continue
        units.append(placeholders.get(part, part))
    return units


def _split_oversized(text: str, limit: int) -> list[str]:
    if len(text) <= limit or text.startswith(("```", "|")):
        return [text]
    sentences = re.split(r"(?<=[。！？.!?；;])\s*|\n+", text)
    result, buffer = [], ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(sentence) > limit:
            if buffer:
                result.append(buffer)
                buffer = ""
            result.extend(sentence[i:i + limit] for i in range(0, len(sentence), limit))
        elif len(buffer) + len(sentence) > limit:
            result.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        result.append(buffer)
    return result
