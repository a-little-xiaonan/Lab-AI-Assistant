"""聊天回合、完成历史版本与回答依据。

Revision ID: 0008_chat_turns_provenance
Revises: 0007_faq_template_audience_scope
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_chat_turns_provenance"
down_revision = "0007_faq_template_audience_scope"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {item["name"] for item in inspector.get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {item["name"] for item in inspector.get_indexes(table)}


def _uniques(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {item["name"] for item in inspector.get_unique_constraints(table) if item["name"]}


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    session_columns = _columns("sessions")
    for name, column in (
        ("summary_through_turn_id", sa.Column("summary_through_turn_id", sa.String(64), nullable=True)),
        ("summary_version", sa.Column("summary_version", sa.String(32), nullable=True)),
        ("history_revision", sa.Column("history_revision", sa.Integer(), nullable=False, server_default="0")),
        ("active_turn_id", sa.Column("active_turn_id", sa.String(64), nullable=True)),
        ("lease_expires_at", sa.Column("lease_expires_at", sa.DateTime(), nullable=True)),
    ):
        if name not in session_columns:
            op.add_column("sessions", column)
    if "ix_sessions_active_turn_id" not in _indexes("sessions"):
        op.create_index("ix_sessions_active_turn_id", "sessions", ["active_turn_id"])

    if "turn_id" not in _columns("messages"):
        op.add_column("messages", sa.Column("turn_id", sa.String(64), nullable=True))
    message_indexes = _indexes("messages")
    if "ix_messages_turn_id" not in message_indexes:
        op.create_index("ix_messages_turn_id", "messages", ["turn_id"])
    if "uq_messages_turn_role" not in message_indexes | _uniques("messages"):
        op.create_index("uq_messages_turn_role", "messages", ["turn_id", "role"], unique=True)

    if "chat_turns" not in tables:
        op.create_table(
            "chat_turns",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False),
            sa.Column("trace_id", sa.String(64), nullable=True),
            sa.Column("request_id", sa.String(64), nullable=True),
            sa.Column("parent_turn_id", sa.String(64), nullable=True),
            sa.Column("history_revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="processing"),
            sa.Column("action", sa.String(24), nullable=False, server_default="answer"),
            sa.Column("target_message_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("plan_version", sa.String(32), nullable=False, server_default="intent-v1"),
            sa.Column("pending_clarification_json", sa.Text(), nullable=True),
            sa.Column("error_code", sa.String(128), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
        for name, columns in (
            ("ix_chat_turns_session_id", ["session_id"]),
            ("ix_chat_turns_trace_id", ["trace_id"]),
            ("ix_chat_turns_request_id", ["request_id"]),
            ("ix_chat_turns_status", ["status"]),
        ):
            op.create_index(name, "chat_turns", columns)

    if "answer_provenance" not in tables:
        op.create_table(
            "answer_provenance",
            sa.Column("assistant_message_id", sa.Integer(), sa.ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("turn_id", sa.String(64), sa.ForeignKey("chat_turns.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("trace_id", sa.String(64), nullable=True),
            sa.Column("schema_version", sa.String(32), nullable=False, server_default="answer-provenance-v1"),
            sa.Column("pipeline_version", sa.String(32), nullable=False, server_default="intent-history-p2"),
            sa.Column("planner_version", sa.String(32), nullable=False, server_default="intent-v1"),
            sa.Column("prompt_fingerprint", sa.String(128), nullable=True),
            sa.Column("answer_kind", sa.String(24), nullable=False),
            sa.Column("basis_status", sa.String(24), nullable=False, server_default="recorded"),
            sa.Column("derived_from_message_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("parts_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("evidence_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_answer_provenance_trace_id", "answer_provenance", ["trace_id"])
        op.create_index("ix_answer_provenance_answer_kind", "answer_provenance", ["answer_kind"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "answer_provenance" in tables:
        op.drop_table("answer_provenance")
    if "chat_turns" in tables:
        op.drop_table("chat_turns")
    if "messages" in tables and "turn_id" in _columns("messages"):
        with op.batch_alter_table("messages") as batch:
            if "uq_messages_turn_role" in _indexes("messages"):
                batch.drop_index("uq_messages_turn_role")
            elif "uq_messages_turn_role" in _uniques("messages"):
                batch.drop_constraint("uq_messages_turn_role", type_="unique")
            if "ix_messages_turn_id" in _indexes("messages"):
                batch.drop_index("ix_messages_turn_id")
            batch.drop_column("turn_id")
    if "sessions" in tables:
        with op.batch_alter_table("sessions") as batch:
            if "ix_sessions_active_turn_id" in _indexes("sessions"):
                batch.drop_index("ix_sessions_active_turn_id")
            for name in (
                "lease_expires_at", "active_turn_id", "history_revision",
                "summary_version", "summary_through_turn_id",
            ):
                if name in _columns("sessions"):
                    batch.drop_column(name)
