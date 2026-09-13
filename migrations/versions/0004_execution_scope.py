"""Persist immutable execution scopes for multi-group runs."""

import sqlalchemy as sa
from alembic import op

revision = "0004_execution_scope"
down_revision = "0003_event_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_scopes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "kind", "window_start", "window_end", name="uq_execution_scopes_identity"
        ),
        sa.CheckConstraint("window_start < window_end", name="ck_execution_scopes_window"),
    )
    op.create_index(
        "idx_execution_scopes_fingerprint", "execution_scopes", ["scope_fingerprint"]
    )
    op.create_table(
        "execution_scope_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "execution_scope_id",
            sa.Integer(),
            sa.ForeignKey("execution_scopes.id"),
            nullable=False,
        ),
        sa.Column("registry_key", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "execution_scope_id",
            "provider",
            "external_id",
            name="uq_execution_scope_groups_external",
        ),
        sa.UniqueConstraint(
            "execution_scope_id", "ordinal", name="uq_execution_scope_groups_ordinal"
        ),
    )
    op.create_index(
        "idx_execution_scope_groups_scope", "execution_scope_groups", ["execution_scope_id"]
    )
    with op.batch_alter_table("collection_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "execution_scope_id",
                sa.Integer(),
                sa.ForeignKey("execution_scopes.id", name="fk_collection_runs_scope"),
                nullable=True,
            )
        )
    with op.batch_alter_table("analysis_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "execution_scope_id",
                sa.Integer(),
                sa.ForeignKey("execution_scopes.id", name="fk_analysis_runs_scope"),
                nullable=True,
            )
        )
    op.create_index(
        "idx_collection_runs_execution_scope",
        "collection_runs",
        ["execution_scope_id"],
    )
    op.create_index(
        "idx_analysis_runs_execution_scope", "analysis_runs", ["execution_scope_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_analysis_runs_execution_scope", table_name="analysis_runs")
    op.drop_index("idx_collection_runs_execution_scope", table_name="collection_runs")
    with op.batch_alter_table("analysis_runs") as batch_op:
        batch_op.drop_column("execution_scope_id")
    with op.batch_alter_table("collection_runs") as batch_op:
        batch_op.drop_column("execution_scope_id")
    op.drop_index("idx_execution_scope_groups_scope", table_name="execution_scope_groups")
    op.drop_table("execution_scope_groups")
    op.drop_index("idx_execution_scopes_fingerprint", table_name="execution_scopes")
    op.drop_table("execution_scopes")
