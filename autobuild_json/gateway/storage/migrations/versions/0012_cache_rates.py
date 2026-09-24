"""Freeze effective customer cache rates without rewriting historical charges."""
from alembic import op
import sqlalchemy as sa

revision = "0012_cache_rates"
down_revision = "0011_catalog_schedule_completion"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("cache_read_micro", "cache_write_micro"):
        op.add_column("requests", sa.Column(name, sa.Numeric(38, 0), nullable=True))
    op.execute("UPDATE requests SET cache_read_micro=input_micro, cache_write_micro=input_micro "
               "WHERE cache_read_micro IS NULL OR cache_write_micro IS NULL")
    for name in ("cache_read_micro", "cache_write_micro"):
        op.alter_column("requests", name, nullable=False)
        op.create_check_constraint(f"requests_{name}_nonnegative", "requests", f"{name} >= 0")


def downgrade():
    raise RuntimeError("Accounting rollback requires an explicit backup/restore procedure")
