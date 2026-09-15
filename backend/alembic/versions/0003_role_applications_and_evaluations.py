"""成员申请与评测运行索引。

Revision ID: 0003_roles_evaluations
Revises: 0002_document_governance
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_roles_evaluations"
down_revision = "0002_document_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "role_applications" not in tables:
        op.create_table(
            "role_applications",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("target_role", sa.String(32), nullable=False, server_default="editor"),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("evidence_text", sa.Text(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("pending_key", sa.String(64), nullable=True, unique=True),
            sa.Column("reviewed_by", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("review_comment", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_role_applications_user_id", "role_applications", ["user_id"])
        op.create_index("ix_role_applications_status", "role_applications", ["status"])
    if "evaluation_runs" not in tables:
        op.create_table(
            "evaluation_runs",
            sa.Column("id", sa.String(96), primary_key=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("mode", sa.String(16), nullable=False),
            sa.Column("dataset_version", sa.String(64), nullable=False),
            sa.Column("dataset_split", sa.String(16), nullable=False, server_default="dev"),
            sa.Column("kb_snapshot", sa.String(128), nullable=False),
            sa.Column("git_commit", sa.String(64), nullable=True),
            sa.Column("model", sa.String(64), nullable=False),
            sa.Column("embedding_model", sa.String(64), nullable=False),
            sa.Column("config_json", sa.Text(), nullable=True),
            sa.Column("metrics_json", sa.Text(), nullable=True),
            sa.Column("result_path", sa.String(512), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "evaluation_runs" in tables:
        op.drop_table("evaluation_runs")
    if "role_applications" in tables:
        op.drop_table("role_applications")
