"""FAQ 模板受众范围改为统一角色等级。

Revision ID: 0007_faq_template_audience_scope
Revises: 0006_faq_templates
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_faq_template_audience_scope"
down_revision = "0006_faq_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "faq_templates" not in tables:
        return
    op.execute("UPDATE faq_templates SET audience_scope = 'guest' WHERE audience_scope = 'public'")
    # P2 稳定前缀新增受众与命名空间，P1 保存的哈希不能再安全复用。
    op.execute("UPDATE faq_templates SET status = 'stale' WHERE status = 'active'")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("faq_templates") as batch:
            batch.alter_column("audience_scope", server_default="guest", existing_type=sa.String(16))
    else:
        op.alter_column("faq_templates", "audience_scope", server_default="guest", existing_type=sa.String(16))


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "faq_templates" not in tables:
        return
    op.execute("UPDATE faq_templates SET audience_scope = 'public' WHERE audience_scope = 'guest'")
    op.execute("UPDATE faq_templates SET status = 'stale' WHERE status = 'active'")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("faq_templates") as batch:
            batch.alter_column("audience_scope", server_default="public", existing_type=sa.String(16))
    else:
        op.alter_column("faq_templates", "audience_scope", server_default="public", existing_type=sa.String(16))
