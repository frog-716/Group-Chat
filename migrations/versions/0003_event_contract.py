"""Separate event semantics from report placement and add event time."""

import sqlalchemy as sa
from alembic import op

revision = "0003_event_contract"
down_revision = "0002_event_driven_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "events",
        "category",
        new_column_name="report_section",
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )
    op.add_column(
        "events",
        sa.Column(
            "event_type", sa.String(length=32), nullable=False, server_default="information"
        ),
    )
    op.add_column("events", sa.Column("event_time", sa.DateTime(timezone=True)))
    op.add_column("events", sa.Column("time_range_start", sa.DateTime(timezone=True)))
    op.add_column("events", sa.Column("time_range_end", sa.DateTime(timezone=True)))
    op.execute(
        """UPDATE events SET event_type = CASE report_section
        WHEN 'howto' THEN 'action'
        WHEN 'pitfalls' THEN 'risk'
        WHEN 'next' THEN 'action'
        ELSE 'information' END"""
    )
    op.execute(
        """UPDATE events SET event_time = (
        SELECT MIN(mv.sent_at)
        FROM event_evidence ee
        JOIN message_versions mv ON mv.id = ee.message_version_id
        WHERE ee.event_id = events.id
        ) WHERE (
        SELECT COUNT(*) FROM event_evidence ee WHERE ee.event_id = events.id
        ) = 1"""
    )
    op.execute(
        """UPDATE events SET
        time_range_start = (
            SELECT MIN(mv.sent_at)
            FROM event_evidence ee
            JOIN message_versions mv ON mv.id = ee.message_version_id
            WHERE ee.event_id = events.id
        ),
        time_range_end = (
            SELECT MAX(mv.sent_at)
            FROM event_evidence ee
            JOIN message_versions mv ON mv.id = ee.message_version_id
            WHERE ee.event_id = events.id
        )
        WHERE (
            SELECT COUNT(*) FROM event_evidence ee WHERE ee.event_id = events.id
        ) > 1"""
    )


def downgrade() -> None:
    op.drop_column("events", "time_range_end")
    op.drop_column("events", "time_range_start")
    op.drop_column("events", "event_time")
    op.drop_column("events", "event_type")
    op.alter_column(
        "events",
        "report_section",
        new_column_name="category",
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )
