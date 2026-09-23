"""Encrypted and tenant-scoped native response handles."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0005_continuations"
down_revision = "0004_proxy_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("continuation_handles",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("handle_digest", sa.LargeBinary(), unique=True, nullable=False),
        sa.Column("customer_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("key_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("route_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("encrypted_upstream_id", pg.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    raise RuntimeError("Continuation rollback requires explicit backup/restore")
