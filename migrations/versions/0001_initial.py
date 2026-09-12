"""Initial append-only message and report schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "external_id", name="uq_chats_provider_external"),
    )
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("chat_external_id", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("requested_since", sa.DateTime(timezone=True)),
        sa.Column("requested_until", sa.DateTime(timezone=True)),
        sa.Column("fetched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(128)),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.Integer(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("platform_message_id", sa.String(255), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chat_id", "platform_message_id", name="uq_messages_chat_platform"),
    )
    op.create_table(
        "message_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column(
            "collection_run_id", sa.Integer(), sa.ForeignKey("collection_runs.id"), nullable=False
        ),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("evidence_id", sa.String(32), nullable=False, unique=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sender_id", sa.String(255), nullable=False),
        sa.Column("sender_name", sa.String(255), nullable=False),
        sa.Column("message_type", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_id", "payload_sha256", name="uq_message_versions_payload"),
    )
    op.create_index("idx_message_versions_sent_at", "message_versions", ["sent_at"])
    op.create_table(
        "sync_cursors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("chat_external_id", sa.String(255), nullable=False),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "chat_external_id", name="uq_sync_cursors_provider_chat"),
    )
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("analyzer", sa.String(128), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("structured_output", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "analysis_run_id", sa.Integer(), sa.ForeignKey("analysis_runs.id"), nullable=False
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "CREATE TRIGGER message_versions_no_update BEFORE UPDATE ON message_versions "
        "BEGIN SELECT RAISE(ABORT, 'message_versions are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER message_versions_no_delete BEFORE DELETE ON message_versions "
        "BEGIN SELECT RAISE(ABORT, 'message_versions are append-only'); END"
    )
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS message_versions_no_delete")
    op.execute("DROP TRIGGER IF EXISTS message_versions_no_update")
    op.drop_table("reports")
    op.drop_table("analysis_runs")
    op.drop_table("sync_cursors")
    op.drop_index("idx_message_versions_sent_at", table_name="message_versions")
    op.drop_table("message_versions")
    op.drop_table("messages")
    op.drop_table("collection_runs")
    op.drop_table("chats")
