"""Track durable schedule acknowledgement without mutating request payloads."""

from alembic import op
import sqlalchemy as sa

revision = "0011_catalog_schedule_completion"
down_revision = "0010_provider_catalog"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("provider_operations", sa.Column("schedule_completed_at", sa.DateTime(timezone=True)))


def downgrade():
    raise RuntimeError("Catalog rollback requires an explicit backup/restore procedure")
