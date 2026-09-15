"""文档治理必填元数据。

Revision ID: 0004_governance_metadata
Revises: 0003_roles_evaluations
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_governance_metadata"
down_revision = "0003_roles_evaluations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "documents" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("documents")}
    for name, column in (
        ("content_owner", sa.Column("content_owner", sa.String(128), nullable=True)),
        ("source_name", sa.Column("source_name", sa.String(255), nullable=True)),
        ("last_reviewed_at", sa.Column("last_reviewed_at", sa.DateTime(), nullable=True)),
    ):
        if name not in columns:
            op.add_column("documents", column)
    op.get_bind().execute(sa.text(
        "UPDATE documents SET source_name=filename WHERE source_name IS NULL"
    ))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("documents")}
    for name in ("last_reviewed_at", "source_name", "content_owner"):
        if name in columns:
            op.drop_column("documents", name)
