"""Optimistic concurrency versions for private configuration editors."""
from alembic import op
import sqlalchemy as sa

revision = "0007_admin_versions"
down_revision = "0006_oauth"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("public_models", "model_bindings", "credentials"):
        op.add_column(table, sa.Column("version", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    raise RuntimeError("Admin version rollback requires explicit migration review")
