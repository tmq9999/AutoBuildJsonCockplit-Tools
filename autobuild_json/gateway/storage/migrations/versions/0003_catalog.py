"""Provider registry and model routing configuration."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0003_catalog"
down_revision = "0002_metering"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("providers",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)))
    op.create_table("config_versions",
        sa.Column("provider_id", pg.UUID(as_uuid=True), sa.ForeignKey("providers.id"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("config", pg.JSONB(), nullable=False))
    op.create_table("discovered_models",
        sa.Column("provider_id", pg.UUID(as_uuid=True), sa.ForeignKey("providers.id"), primary_key=True),
        sa.Column("model_id", sa.Text(), primary_key=True))
    op.create_table("credentials",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_id", pg.UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("encrypted_secret", pg.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("health", sa.Text(), nullable=False, server_default="active"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)))
    op.create_table("public_models",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("cursor", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_table("model_aliases",
        sa.Column("alias", sa.Text(), primary_key=True),
        sa.Column("model_id", sa.Text(), sa.ForeignKey("public_models.id"), nullable=False))
    op.create_table("model_bindings",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_id", pg.UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("credential_id", pg.UUID(as_uuid=True), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("model_id", sa.Text(), sa.ForeignKey("public_models.id"), nullable=False),
        sa.Column("config", pg.JSONB(), nullable=False))


def downgrade():
    raise RuntimeError("Catalog rollback requires an explicit backup/restore procedure")
