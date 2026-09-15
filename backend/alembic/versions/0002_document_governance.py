"""文档治理、版本、质量检查和审计扩展。

Revision ID: 0002_document_governance
Revises: 0001_existing_schema_baseline
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_document_governance"
down_revision = "0001_existing_schema_baseline"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return _has_table(table) and column in {item["name"] for item in _inspector().get_columns(table)}


def _add_column(table: str, column: sa.Column) -> None:
    if not _has_column(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    # create_all 会为新数据库创建完整模型；以下操作负责升级现有数据库。
    if not _has_table("document_versions"):
        op.create_table(
            "document_versions",
            sa.Column("id", sa.String(96), primary_key=True),
            sa.Column("document_id", sa.String(64), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("file_path", sa.String(512), nullable=False),
            sa.Column("file_hash", sa.String(64), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column("change_summary", sa.Text(), nullable=True),
            sa.Column("processing_status", sa.String(16), nullable=False, server_default="processing"),
            sa.Column("review_status", sa.String(24), nullable=False, server_default="draft"),
            sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_by", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("document_id", "version_no", name="uq_document_versions_doc_no"),
        )
        op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
        op.create_index("ix_document_versions_file_hash", "document_versions", ["file_hash"])
        op.create_index("ix_document_versions_content_hash", "document_versions", ["content_hash"])
        op.create_index("ix_document_versions_processing_status", "document_versions", ["processing_status"])

    if not _has_table("document_version_chunks"):
        op.create_table(
            "document_version_chunks",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("document_version_id", sa.String(96), sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("doc_id", sa.String(64), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kb_id", sa.String(64), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("char_length", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("page", sa.Integer(), nullable=True),
            sa.Column("slide_number", sa.Integer(), nullable=True),
            sa.Column("sheet_name", sa.String(255), nullable=True),
            sa.Column("row_range", sa.String(32), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("document_version_id", "chunk_index", name="uq_version_chunks_version_index"),
        )
        op.create_index("ix_document_version_chunks_version", "document_version_chunks", ["document_version_id"])
        op.create_index("ix_document_version_chunks_doc", "document_version_chunks", ["doc_id"])
        op.create_index("ix_document_version_chunks_kb", "document_version_chunks", ["kb_id"])

    if not _has_table("document_quality_checks"):
        op.create_table(
            "document_quality_checks",
            sa.Column("id", sa.String(96), primary_key=True),
            sa.Column("document_version_id", sa.String(96), sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("check_type", sa.String(32), nullable=False),
            sa.Column("severity", sa.String(16), nullable=False),
            sa.Column("result", sa.String(16), nullable=False),
            sa.Column("details_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_document_quality_checks_version", "document_quality_checks", ["document_version_id"])

    if not _has_table("document_reviews"):
        op.create_table(
            "document_reviews",
            sa.Column("id", sa.String(96), primary_key=True),
            sa.Column("document_version_id", sa.String(96), sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("reviewer_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("action", sa.String(24), nullable=False),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_document_reviews_version", "document_reviews", ["document_version_id"])

    for column in (
        sa.Column("governance_status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("sensitivity_level", sa.String(16), nullable=False, server_default="guest"),
        sa.Column("uploader_id", sa.String(64), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("current_version_id", sa.String(96), nullable=True),
        sa.Column("published_version_id", sa.String(96), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("published_by", sa.String(64), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("effective_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("legacy_unreviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    ):
        _add_column("documents", column)

    _add_column("chunks", sa.Column("document_version_id", sa.String(96), nullable=True))

    for column in (
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("actor_role", sa.String(32), nullable=True),
        sa.Column("result", sa.String(16), nullable=False, server_default="success"),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("ip_hash", sa.String(64), nullable=True),
        sa.Column("user_agent_summary", sa.String(255), nullable=True),
    ):
        _add_column("audit_logs", column)

    bind = op.get_bind()
    bind.execute(sa.text("UPDATE documents SET governance_status = CASE WHEN status='ready' THEN 'published' ELSE 'draft' END WHERE governance_status IS NULL OR governance_status='draft'"))
    bind.execute(sa.text("UPDATE documents SET legacy_unreviewed=1, review_comment='历史数据迁移，待复核' WHERE status='ready'"))
    bind.execute(sa.text("UPDATE documents SET updated_at=created_at WHERE updated_at IS NULL"))


def downgrade() -> None:
    # 回滚仅撤销本阶段结构；执行前必须先备份业务数据。
    for table in ("document_reviews", "document_quality_checks", "document_version_chunks", "document_versions"):
        if _has_table(table):
            op.drop_table(table)
    for column in (
        "document_version_id",
    ):
        if _has_column("chunks", column):
            op.drop_column("chunks", column)
    for column in (
        "request_id", "actor_role", "result", "reason_code", "ip_hash", "user_agent_summary",
    ):
        if _has_column("audit_logs", column):
            op.drop_column("audit_logs", column)
    for column in (
        "governance_status", "sensitivity_level", "uploader_id", "current_version",
        "current_version_id", "published_version_id", "lock_version", "reviewed_by",
        "reviewed_at", "published_by", "published_at", "effective_at", "expires_at",
        "review_comment", "legacy_unreviewed", "deleted_at", "updated_at",
    ):
        if _has_column("documents", column):
            op.drop_column("documents", column)
