"""初始化 PostgreSQL Schema。"""

from alembic import op

from app.infrastructure.postgresql import _postgres_schema

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in _postgres_schema():
        op.execute(statement)


def downgrade() -> None:
    for table in (
        "outbox_events", "chunks_fts", "wiki_page_issues", "wiki_page_contributions",
        "wiki_build_stages", "wiki_builds", "wiki_page_links", "wiki_page_sources",
        "wiki_folders", "wiki_pages", "chat_messages", "processing_stages",
        "ingestion_tasks", "chunks", "parent_chunks", "knowledges", "knowledge_bases",
    ):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
