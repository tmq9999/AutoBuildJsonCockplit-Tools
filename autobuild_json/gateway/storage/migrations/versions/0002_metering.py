"""Quota requests, window buckets, immutable settlements and attempt metadata."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0002_metering"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("quota_buckets",
        sa.Column("key_id", pg.UUID(as_uuid=True), sa.ForeignKey("api_keys.id"), primary_key=True),
        sa.Column("window_kind", sa.Text(), primary_key=True),
        sa.Column("period", sa.Text(), primary_key=True),
        sa.Column("spent", sa.Numeric(38, 0), nullable=False, server_default="0"),
        sa.Column("held", sa.Numeric(38, 0), nullable=False, server_default="0"),
        sa.CheckConstraint("spent >= 0 AND held >= 0"),
        sa.CheckConstraint("window_kind IN ('total', 'day', 'month')"))
    op.create_table("requests",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("key_id", pg.UUID(as_uuid=True), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("periods", pg.JSONB(), nullable=False),
        sa.Column("input_micro", sa.Numeric(38, 0), nullable=False),
        sa.Column("output_micro", sa.Numeric(38, 0), nullable=False),
        sa.Column("input_bound", sa.BigInteger(), nullable=False),
        sa.Column("output_bound", sa.BigInteger(), nullable=False),
        sa.Column("hold", sa.Numeric(38, 0), nullable=False),
        sa.Column("usage", pg.JSONB()),
        sa.Column("reason", sa.Text()),
        sa.Column("idempotency_digest", sa.LargeBinary()),
        sa.Column("payload_digest", sa.LargeBinary()),
        sa.Column("idempotency_expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('reserved','dispatched','usage_pending','completed','released','adjusted')"),
        sa.CheckConstraint("hold >= 0 AND input_bound >= 0 AND output_bound >= 0"),
        sa.UniqueConstraint("key_id", "idempotency_digest"))
    op.create_index("requests_admission_key", "requests", ["key_id", "admitted_at"])
    op.create_table("attempts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", pg.UUID(as_uuid=True), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("status", sa.Text(), nullable=False, server_default="started"),
        sa.Column("usage", pg.JSONB()),
        sa.Column("cost", pg.JSONB()),
        sa.CheckConstraint("status IN ('started','rejected','completed','unknown')"))
    op.create_table("usage_ledger",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", pg.UUID(as_uuid=True), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("entry_kind", sa.Text(), nullable=False),
        sa.Column("amount_micro", sa.Numeric(38, 0), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("request_id", "entry_kind"),
        sa.CheckConstraint("amount_micro >= 0"))
    op.create_table("upstream_budgets",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("budget_limit", sa.Numeric(38, 12)),
        sa.Column("spent", sa.Numeric(38, 12), nullable=False, server_default="0"),
        sa.Column("held", sa.Numeric(38, 12), nullable=False, server_default="0"),
        sa.CheckConstraint("spent >= 0 AND held >= 0 AND (budget_limit IS NULL OR budget_limit >= 0)"))
    op.create_table("upstream_reservations",
        sa.Column("attempt_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("budget_id", pg.UUID(as_uuid=True), sa.ForeignKey("upstream_budgets.id"), nullable=False),
        sa.Column("amount", sa.Numeric(38, 12), nullable=False),
        sa.Column("actual", sa.Numeric(38, 12)),
        sa.Column("state", sa.Text(), nullable=False, server_default="reserved"),
        sa.CheckConstraint("amount >= 0 AND (actual IS NULL OR actual >= 0)"),
        sa.CheckConstraint("state IN ('reserved','pending','settled')"))


def downgrade():
    raise RuntimeError("Accounting rollback is destructive; restore a verified backup instead")
