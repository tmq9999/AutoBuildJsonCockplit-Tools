"""Shared proxy profiles and operation-scoped fenced claims."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0004_proxy_leases"
down_revision = "0003_catalog"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("proxy_health",
        sa.Column("resource", sa.Text(), primary_key=True),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.create_table("proxy_leases",
        sa.Column("resource", sa.Text(), primary_key=True),
        sa.Column("owner", pg.UUID(as_uuid=True)),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("hard_deadline", sa.DateTime(timezone=True)))
    op.create_table("proxy_profiles",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("encrypted_entries", pg.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    raise RuntimeError("Proxy rollback requires explicit backup/restore")
