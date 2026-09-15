"""现有数据库结构基线。

Revision ID: 0001_existing_schema_baseline
Revises: None
"""
from __future__ import annotations

revision = "0001_existing_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """现有表由历史版本创建，本基线只建立版本边界。"""


def downgrade() -> None:
    """基线不删除历史业务表。"""
