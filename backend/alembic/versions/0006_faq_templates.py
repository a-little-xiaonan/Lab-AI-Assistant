"""公开 FAQ 模板与证据卡。

Revision ID: 0006_faq_templates
Revises: 0005_jobs_feedback
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_faq_templates"
down_revision = "0005_jobs_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "faq_templates" not in tables:
        op.create_table(
            "faq_templates",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("code", sa.String(64), nullable=False, unique=True),
            sa.Column("title", sa.String(128), nullable=False),
            sa.Column("kb_scope_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("audience_scope", sa.String(16), nullable=False, server_default="public"),
            sa.Column("template_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("prompt_policy_version", sa.String(64), nullable=False, server_default="faq-policy-v1"),
            sa.Column("knowledge_snapshot", sa.String(128), nullable=False),
            sa.Column("stable_prompt_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("created_by", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_faq_templates_code", "faq_templates", ["code"])
        op.create_index("ix_faq_templates_audience_scope", "faq_templates", ["audience_scope"])
        op.create_index("ix_faq_templates_status", "faq_templates", ["status"])
        op.create_index("ix_faq_templates_snapshot", "faq_templates", ["knowledge_snapshot"])
    if "faq_template_evidences" not in tables:
        op.create_table(
            "faq_template_evidences",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("template_id", sa.String(64), sa.ForeignKey("faq_templates.id", ondelete="CASCADE"), nullable=False),
            sa.Column("doc_id", sa.String(64), nullable=False),
            sa.Column("document_version_id", sa.String(96), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("excerpt", sa.Text(), nullable=False),
            sa.Column("citation_label", sa.String(255), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("template_id", "doc_id", "chunk_index", name="uq_faq_evidence_chunk"),
        )
        op.create_index("ix_faq_evidences_template", "faq_template_evidences", ["template_id"])
        op.create_index("ix_faq_evidences_doc", "faq_template_evidences", ["doc_id"])
        op.create_index("ix_faq_evidences_version", "faq_template_evidences", ["document_version_id"])
    if "faq_template_aliases" not in tables:
        op.create_table(
            "faq_template_aliases",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("template_id", sa.String(64), sa.ForeignKey("faq_templates.id", ondelete="CASCADE"), nullable=False),
            sa.Column("normalized_query", sa.String(500), nullable=False),
            sa.Column("match_mode", sa.String(16), nullable=False, server_default="exact"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("template_id", "normalized_query", name="uq_faq_alias_query"),
        )
        op.create_index("ix_faq_aliases_template", "faq_template_aliases", ["template_id"])
        op.create_index("ix_faq_aliases_query", "faq_template_aliases", ["normalized_query"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("faq_template_aliases", "faq_template_evidences", "faq_templates"):
        if table in tables:
            op.drop_table(table)
