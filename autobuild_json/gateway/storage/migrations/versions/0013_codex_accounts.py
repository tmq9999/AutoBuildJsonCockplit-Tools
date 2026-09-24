"""Durable Codex account quota snapshots, pools and reset operations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0013_codex_accounts"
down_revision = "0012_cache_rates"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("codex_account_state",
        sa.Column("credential_id", pg.UUID(as_uuid=True), sa.ForeignKey("credentials.id"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("usage_snapshot", pg.JSONB()), sa.Column("credits_snapshot", pg.JSONB()),
        sa.Column("fetched_at", sa.DateTime(timezone=True)), sa.Column("credits_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)), sa.Column("last_error", sa.Text()),
        sa.Column("usage_error", sa.Text()), sa.Column("credits_error", sa.Text()),
        sa.Column("usage_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credits_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("snapshot_refresh_started_at", sa.DateTime(timezone=True)),
        sa.Column("refresh_started_at", sa.DateTime(timezone=True)),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("refresh_deadline", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)), sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("version >= 1"), sa.CheckConstraint("generation >= 0"), sa.CheckConstraint("failure_count >= 0"))
    op.create_table("codex_reset_requests",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("credential_id", pg.UUID(as_uuid=True), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("redeem_request_id", pg.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("payload_digest", sa.LargeBinary(), nullable=False),
        sa.Column("credits_version", sa.Integer(), nullable=False), sa.Column("stamp", pg.JSONB(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)), sa.Column("receipt_at", sa.DateTime(timezone=True)),
        sa.Column("dispatch_generation", sa.BigInteger()),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("upstream_status", sa.Integer()), sa.Column("result_code", sa.Text()),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", pg.JSONB()),
        sa.CheckConstraint("state IN ('prepared','dispatched','rejected','unknown','succeeded','succeeded_refresh_failed')"),
        sa.CheckConstraint("generation >= 1 AND version >= 1"),
        sa.CheckConstraint("credits_version >= 1 AND (dispatch_generation IS NULL OR dispatch_generation >= 0)"),
        sa.CheckConstraint("deadline > requested_at"), sa.CheckConstraint("octet_length(payload_digest) = 32"),
        sa.CheckConstraint("upstream_status IS NULL OR upstream_status BETWEEN 100 AND 599"))
    op.create_index("codex_reset_active_credential", "codex_reset_requests", ["credential_id"],
                    postgresql_where=sa.text("state IN ('prepared','dispatched','unknown','succeeded_refresh_failed')"), unique=True)
    op.create_table("account_pool_policies",
        sa.Column("model_id", sa.Text(), sa.ForeignKey("public_models.id"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("policy", pg.JSONB(), nullable=False), sa.CheckConstraint("version >= 1"))
    op.create_table("account_session_bindings",
        sa.Column("scope_digest", sa.LargeBinary(), primary_key=True),
        sa.Column("customer_id", pg.UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("key_id", pg.UUID(as_uuid=True), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("model_id", sa.Text(), sa.ForeignKey("public_models.id"), nullable=False),
        sa.Column("binding_id", pg.UUID(as_uuid=True), sa.ForeignKey("model_bindings.id"), nullable=False),
        sa.Column("credential_id", pg.UUID(as_uuid=True), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("octet_length(scope_digest) = 32"), sa.CheckConstraint("version >= 1"))
    op.create_index("account_session_bindings_expiry", "account_session_bindings", ["expires_at"])
    op.create_table("key_quota_adjustments",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("key_id", pg.UUID(as_uuid=True), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("version_before", sa.Integer(), nullable=False), sa.Column("version_after", sa.Integer(), nullable=False),
        sa.Column("before_limit_micro", sa.Numeric(38, 0)), sa.Column("after_limit_micro", sa.Numeric(38, 0)),
        sa.Column("amount_micro", sa.Numeric(38, 0), nullable=False), sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("version_before >= 1 AND version_after = version_before + 1"),
        sa.CheckConstraint("amount_micro >= 0"),
        sa.CheckConstraint("before_limit_micro IS NULL OR before_limit_micro >= 0"),
        sa.CheckConstraint("after_limit_micro IS NULL OR after_limit_micro >= 0"))
    op.execute("""CREATE FUNCTION codex_adjustment_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'immutable_quota_adjustment'; END $$""")
    op.execute("CREATE TRIGGER codex_adjustment_no_rewrite BEFORE UPDATE OR DELETE ON key_quota_adjustments "
               "FOR EACH ROW EXECUTE FUNCTION codex_adjustment_append_only()")
    for name, type_ in (("provider_id", pg.UUID(as_uuid=True)), ("credential_id", pg.UUID(as_uuid=True)),
                        ("binding_id", pg.UUID(as_uuid=True)), ("config_version", sa.Integer())):
        op.add_column("attempts", sa.Column(name, type_))
    op.create_foreign_key("attempts_provider_fk", "attempts", "providers", ["provider_id"], ["id"])
    op.create_foreign_key("attempts_credential_fk", "attempts", "credentials", ["credential_id"], ["id"])
    op.create_foreign_key("attempts_binding_fk", "attempts", "model_bindings", ["binding_id"], ["id"])
    op.create_check_constraint("attempts_config_version_positive", "attempts", "config_version IS NULL OR config_version >= 1")
    op.create_index("attempts_credential_started", "attempts", ["credential_id", "started_at"])


def downgrade():
    raise RuntimeError("Codex account rollback requires an explicit backup/restore procedure")
