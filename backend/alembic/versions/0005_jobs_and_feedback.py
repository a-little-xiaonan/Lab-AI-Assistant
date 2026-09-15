"""持久任务、推荐问题与回答反馈。

Revision ID: 0005_jobs_feedback
Revises: 0004_governance_metadata
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_jobs_feedback"
down_revision = "0004_governance_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "background_jobs" not in tables:
        op.create_table(
            "background_jobs",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("job_type", sa.String(32), nullable=False),
            sa.Column("resource_type", sa.String(32), nullable=False),
            sa.Column("resource_id", sa.String(96), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("progress_current", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("progress_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("idempotency_key", sa.String(255), nullable=False),
            sa.Column("active_key", sa.String(255), nullable=True, unique=True),
            sa.Column("requested_by", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("queue_job_id", sa.String(128), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("request_id", sa.String(64), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_background_jobs_status_created", "background_jobs", ["status", "created_at"])
        op.create_index("ix_background_jobs_resource", "background_jobs", ["resource_type", "resource_id"])
        op.create_index("ix_background_jobs_requested", "background_jobs", ["requested_by", "created_at"])
        op.create_index("ix_background_jobs_job_type", "background_jobs", ["job_type"])
        op.create_index("ix_background_jobs_heartbeat_at", "background_jobs", ["heartbeat_at"])
        op.create_index("ix_background_jobs_request_id", "background_jobs", ["request_id"])
    if "suggested_questions" not in tables:
        op.create_table(
            "suggested_questions",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("category", sa.String(64), nullable=False),
            sa.Column("question", sa.String(500), nullable=False),
            sa.Column("required_level", sa.String(16), nullable=False, server_default="guest"),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("source_type", sa.String(16), nullable=False, server_default="manual"),
            sa.Column("created_by", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_suggested_questions_category", "suggested_questions", ["category"])
        op.create_index("ix_suggested_questions_level", "suggested_questions", ["required_level"])
        op.create_index("ix_suggested_questions_enabled", "suggested_questions", ["enabled"])
    if "answer_feedback" not in tables:
        op.create_table(
            "answer_feedback",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
            sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False),
            sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("anonymous_id", sa.String(64), nullable=True),
            sa.Column("identity_key", sa.String(96), nullable=False),
            sa.Column("rating", sa.String(16), nullable=False),
            sa.Column("reason_code", sa.String(32), nullable=True),
            sa.Column("comment", sa.String(1000), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("reviewer_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("resolution_note", sa.String(1000), nullable=True),
            sa.Column("snapshot_json", sa.Text(), nullable=True),
            sa.Column("evaluation_candidate", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("message_id", "identity_key", name="uq_feedback_message_identity"),
        )
        for name, columns in (
            ("ix_answer_feedback_message_id", ["message_id"]),
            ("ix_answer_feedback_session_id", ["session_id"]),
            ("ix_answer_feedback_user_id", ["user_id"]),
            ("ix_answer_feedback_anonymous_id", ["anonymous_id"]),
            ("ix_answer_feedback_status", ["status"]),
            ("ix_answer_feedback_reason_code", ["reason_code"]),
            ("ix_answer_feedback_evaluation_candidate", ["evaluation_candidate"]),
        ):
            op.create_index(name, "answer_feedback", columns)


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("answer_feedback", "suggested_questions", "background_jobs"):
        if table in tables:
            op.drop_table(table)
