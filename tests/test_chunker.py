from app.services.chunker import build_parent_chunks, split_document


def test_heading_table_and_page_are_preserved():
    text = """<!-- page:2 -->

# 标题

第一段内容。第二句话。

| A | B |
| --- | --- |
| 1 | 2 |
"""
    chunks = split_document(text, 100, 10)
    assert chunks
    assert chunks[0].heading_path == "标题"
    assert chunks[0].page_number == 2
    assert "| A | B |" in "\n".join(x.content for x in chunks)


def test_long_content_is_split():
    chunks = split_document("# T\n\n" + "这是长句。" * 100, 120, 10)
    assert len(chunks) > 2
    assert all(x.heading_path == "T" for x in chunks)


def test_parent_chunks_are_bounded_and_children_reference_them():
    text = "# T\n\n" + "\n\n".join(f"第{i}段。" * 12 for i in range(8))
    chunks = split_document(text, 100, 10, parent_chunk_size=220)
    parents = build_parent_chunks(chunks)

    assert len(parents) > 1
    assert {chunk.parent_index for chunk in chunks} == {parent.parent_index for parent in parents}
    assert all(len(parent.content) <= 220 for parent in parents)
    parent_map = {parent.parent_index: parent for parent in parents}
    assert all(chunk.content in parent_map[chunk.parent_index].content for chunk in chunks)
