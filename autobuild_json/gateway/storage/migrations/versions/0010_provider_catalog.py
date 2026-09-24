"""Add private provider catalog sources, operations, snapshots and decisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0010_provider_catalog"
down_revision = "0009_provider_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("upstream_budgets", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))

    op.create_table(
        "catalog_sources",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_id", pg.UUID(as_uuid=True), sa.ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("credential_id", pg.UUID(as_uuid=True), sa.ForeignKey("credentials.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("schedule_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latest_successful_run_id", pg.UUID(as_uuid=True)),
        sa.Column("previous_successful_run_id", pg.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("provider_id", "credential_id", name="catalog_sources_provider_credential"),
        sa.CheckConstraint("mode IN ('openai_single','openai_cursor','anthropic','gemini','ollama')", name="catalog_sources_mode"),
        sa.CheckConstraint("version >= 1 AND failure_count >= 0 AND interval_seconds BETWEEN 300 AND 604800", name="catalog_sources_bounds"),
    )
    op.create_index("catalog_sources_due", "catalog_sources", ["next_run_at"],
                    postgresql_where=sa.text("enabled AND schedule_enabled AND next_run_at IS NOT NULL"))

    op.create_table(
        "provider_operations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", pg.UUID(as_uuid=True), sa.ForeignKey("catalog_sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload_digest", sa.LargeBinary(), nullable=False),
        sa.Column("input", pg.JSONB(), nullable=False),
        sa.Column("expected", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("queued_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("owner", pg.UUID(as_uuid=True)),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_id", pg.UUID(as_uuid=True), unique=True),
        sa.Column("budget_id", pg.UUID(as_uuid=True), sa.ForeignKey("upstream_budgets.id", ondelete="RESTRICT")),
        sa.Column("probe_cost_snapshot", pg.JSONB()),
        sa.Column("upper_cost", sa.Numeric(38, 12)),
        sa.Column("usage", pg.JSONB()),
        sa.Column("cost", pg.JSONB()),
        sa.Column("result", pg.JSONB()),
        sa.Column("error_code", sa.Text()),
        sa.Column("upstream_status", sa.Integer()),
        sa.Column("retry_after", sa.Integer()),
        sa.Column("settlement_source", sa.Text()),
        sa.Column("reconcile_digest", sa.LargeBinary()),
        sa.Column("retention_protected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("kind IN ('discover','check','probe')", name="provider_operations_kind"),
        sa.CheckConstraint("state IN ('queued','running','succeeded','failed','cancelled','stale','usage_pending')", name="provider_operations_state"),
        sa.CheckConstraint("version >= 1 AND generation >= 0 AND (upper_cost IS NULL OR upper_cost >= 0)", name="provider_operations_bounds"),
    )
    op.create_index("provider_operations_active_source", "provider_operations", ["source_id"], unique=True,
                    postgresql_where=sa.text("state IN ('queued','running')"))
    op.create_index("provider_operations_state_created", "provider_operations", ["state", "created_at"])
    op.create_index("provider_operations_source_created", "provider_operations", ["source_id", "created_at"])

    op.create_foreign_key("catalog_sources_latest_run", "catalog_sources", "provider_operations",
                          ["latest_successful_run_id"], ["id"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED")
    op.create_foreign_key("catalog_sources_previous_run", "catalog_sources", "provider_operations",
                          ["previous_successful_run_id"], ["id"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED")

    op.create_table(
        "provider_operation_claims",
        sa.Column("client_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", pg.UUID(as_uuid=True), sa.ForeignKey("provider_operations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payload_digest", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
    )
    op.create_index("provider_operation_claims_operation", "provider_operation_claims", ["operation_id"])

    op.create_table(
        "catalog_entries",
        sa.Column("run_id", pg.UUID(as_uuid=True), sa.ForeignKey("provider_operations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("upstream_id", sa.Text(), primary_key=True),
        sa.Column("metadata", pg.JSONB(), nullable=False),
        sa.Column("digest", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("char_length(upstream_id) BETWEEN 1 AND 200", name="catalog_entries_upstream_id_bounds"),
    )

    op.create_table(
        "catalog_decisions",
        sa.Column("source_id", pg.UUID(as_uuid=True), sa.ForeignKey("catalog_sources.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("upstream_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("ignored", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("binding_id", pg.UUID(as_uuid=True), sa.ForeignKey("model_bindings.id", ondelete="RESTRICT")),
        sa.Column("published_run_id", pg.UUID(as_uuid=True), sa.ForeignKey("provider_operations.id", ondelete="RESTRICT")),
        sa.Column("published_digest", sa.LargeBinary()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("version >= 1 AND char_length(upstream_id) BETWEEN 1 AND 200", name="catalog_decisions_bounds"),
    )
    op.create_index("catalog_decisions_binding", "catalog_decisions", ["binding_id"])
    op.create_index("catalog_decisions_published_run", "catalog_decisions", ["published_run_id"])

    op.create_table(
        "catalog_publications",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", pg.UUID(as_uuid=True), sa.ForeignKey("catalog_sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_id", pg.UUID(as_uuid=True), sa.ForeignKey("provider_operations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("payload_digest", sa.LargeBinary(), nullable=False),
        sa.Column("result", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
    )


def downgrade():
    raise RuntimeError("Catalog rollback requires an explicit backup/restore procedure")
