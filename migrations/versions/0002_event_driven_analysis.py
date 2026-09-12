"""Add run-scoped events and exact analysis input lineage."""

import sqlalchemy as sa
from alembic import op

revision = "0002_event_driven_analysis"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "analysis_runs",
        "input_sha256",
        new_column_name="input_hash",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.add_column(
        "analysis_runs",
        sa.Column("model", sa.String(length=128), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "analysis_runs",
        sa.Column(
            "prompt_version", sa.String(length=128), nullable=False, server_default="legacy"
        ),
    )
    op.add_column(
        "analysis_runs",
        sa.Column(
            "schema_version", sa.String(length=128), nullable=False, server_default="mvp-v1"
        ),
    )
    op.create_table(
        "analysis_inputs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "analysis_run_id", sa.Integer(), sa.ForeignKey("analysis_runs.id"), nullable=False
        ),
        sa.Column(
            "message_version_id",
            sa.Integer(),
            sa.ForeignKey("message_versions.id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "analysis_run_id", "message_version_id", name="uq_analysis_inputs_run_version"
        ),
        sa.UniqueConstraint(
            "analysis_run_id", "ordinal", name="uq_analysis_inputs_run_ordinal"
        ),
    )
    op.create_index(
        "idx_analysis_inputs_message_version", "analysis_inputs", ["message_version_id"]
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "analysis_run_id", sa.Integer(), sa.ForeignKey("analysis_runs.id"), nullable=False
        ),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("analysis_kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_events_analysis_run", "events", ["analysis_run_id"])
    op.create_table(
        "event_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column(
            "message_version_id",
            sa.Integer(),
            sa.ForeignKey("message_versions.id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint("event_id", "message_version_id", name="uq_event_evidence_version"),
        sa.UniqueConstraint("event_id", "ordinal", name="uq_event_evidence_ordinal"),
    )
    op.create_index(
        "idx_event_evidence_message_version", "event_evidence", ["message_version_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_event_evidence_message_version", table_name="event_evidence")
    op.drop_table("event_evidence")
    op.drop_index("idx_events_analysis_run", table_name="events")
    op.drop_table("events")
    op.drop_index("idx_analysis_inputs_message_version", table_name="analysis_inputs")
    op.drop_table("analysis_inputs")
    op.drop_column("analysis_runs", "schema_version")
    op.drop_column("analysis_runs", "prompt_version")
    op.drop_column("analysis_runs", "model")
    op.alter_column(
        "analysis_runs",
        "input_hash",
        new_column_name="input_sha256",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
